"""Scout — mechanical candidate discovery.

Funnel:  earnings calendar (all symbols)
           └─ surprise screen (EPS/revenue thresholds)       [cheap, no I/O]
                └─ enrichment (prices, profile; bounded)     [free-tier APIs]
                     └─ entry gates                          [doctrine]
                          └─ ranked shortlist → tribunal

Every stage's counts are recorded so the funnel itself is auditable:
"how many candidates did the screen see, and what fraction survived?"
is a first-class metric of the system, same as P&L.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import (
    CandidateBrief,
    Catalyst,
    CatalystType,
    GateReport,
    ScreenedCandidate,
    ScreenedCandidateStage,
    utc_date,
)
from hawkeye.contracts.stocks import ConsensusSnapshot, EarningsPrint
from hawkeye.gates.entry_gates import run_entry_gates
from hawkeye.marketdata.base import MarketDataProvider
from hawkeye.marketdata.consensus import shift_after_print
from hawkeye.marketdata.snapshot import build_brief
from hawkeye.scout.earnings import (
    ScreenedEvent,
    parse_calendar,
    score_candidate,      # re-exported: the ranking score lives with the screen
    screen_events,
)
from hawkeye.scout.prereg import resolve_stock
from hawkeye.scout.triage import is_investigation_target, triage_from_gates
from hawkeye.scout.quality import (
    EarningsQuality,
    assess_earnings,
    describe_quality_en,
    print_from_event,
    reconstructed_consensus,
)
from hawkeye.scout.numbers import (
    NumbersStats,
    WhispersReader,
    read_numbers,
)
from hawkeye.scout.waiting import held_reason, wait_expired


@dataclass
class ScoutCandidate:
    ticker: str
    event_date: date
    eps_surprise_pct: float
    revenue_surprise_pct: Optional[float]
    score: float = 0.0
    score_version: str = "partial_no_gap"  # "full" once gap_on_event_pct is known
    # How far the screen trusts its own numbers. An untrusted percentage is
    # still recorded — the drop review needs to know what the screen saw —
    # but it earns no score and is never handed to the tribunal as a fact.
    eps_surprise_trusted: bool = True
    revenue_surprise_trusted: bool = True
    conflicting_estimates: bool = False
    # Which vendor BOTH figures above came from ("calendar" or "whispers"),
    # and what the calendar had said when the feed replaced it. Recorded on
    # every candidate so a later review can tell a decision made on the feed's
    # numbers from one made on the calendar's.
    numbers_source: str = "calendar"
    # Why the feed's figures are not the ones above (hawkeye/scout/numbers.py).
    numbers_reason: str = ""
    calendar_eps_surprise_pct: Optional[float] = None
    # Why this print could not be ranked at all, and whether the wait for its
    # numbers has run out (hawkeye/scout/waiting.py). "" for every candidate
    # that WAS judged — a held name never reaches enrichment or the gates, so
    # it is not a rejection and must not be counted as one.
    held_reason: str = ""
    held_expired: bool = False
    price: Optional[float] = None
    price_asof: Optional[date] = None
    brief: Optional[CandidateBrief] = None
    gate_report: Optional[GateReport] = None
    reject_reason: str = ""
    # The three-leg reading of the quarter (EPS / revenue / guidance), when a
    # stock store was supplied. None means the funnel ran without one and the
    # old single-leg surprise decided the ranking — a different fact from
    # "all three legs were checked and found nothing".
    quality: Optional[EarningsQuality] = None


@dataclass
class ScoutResult:
    scan_start: date
    scan_end: date
    scanned: int                 # raw calendar entries
    screened: int                # survived the surprise screen
    enriched: int                # fetched prices/profile for
    passed: list[ScoutCandidate] = field(default_factory=list)   # ranked
    rejected: list[ScoutCandidate] = field(default_factory=list)
    capped: list[ScoutCandidate] = field(default_factory=list)  # never enriched (scout_max_enrich)
    # Prints whose own numbers had not arrived: never judged, so never
    # rejected. Kept apart from `rejected` because a held name is a fact about
    # our data and a rejected one is a fact about the company.
    held: list[ScoutCandidate] = field(default_factory=list)
    duplicates: int = 0          # already recorded by an earlier scan
    window_truncated: bool = False  # the lookback cap bounded the window
    numbers: NumbersStats = field(default_factory=NumbersStats)
    # The attempt ceiling stopped the walk before the gate-passed pool was
    # full. Means the shortlist is short because the budget ran out, not
    # because the calendar was quiet — the two must not read the same.
    enrichment_ceiling_hit: bool = False

    def funnel(self) -> dict:
        return {"scanned": self.scanned, "screened": self.screened,
                "duplicates": self.duplicates, "held": len(self.held),
                "enriched": self.enriched, "gate_passed": len(self.passed)}


# --- scan window (docs/design/MASTER_OVERVIEW.ja.md §5.2(1)) -----------------------

@dataclass(frozen=True)
class ScanWindow:
    """The range of earnings days one scan covers.

    Derived from the previous run rather than fixed, because runs are manual
    and irregular — no scheduler exists. A fixed narrow window would silently
    drop the earnings of every day the user didn't run, permanently; a fixed
    wide one re-reads the same days every time. `truncated` says the cap
    bounded the lookback, so earlier days went unscanned and it is known.
    """
    start: date
    end: date
    truncated: bool = False

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def previous_business_day(day: date) -> date:
    """The event-day close has to be final before a candidate can be ranked
    (the score rewards a confirming event-day move), and US earnings land
    pre-market or after the close — so today's reporters wait for the next
    run rather than being ranked on an unfinished session."""
    d = day - timedelta(days=1)
    while d.weekday() >= 5:      # Saturday/Sunday
        d -= timedelta(days=1)
    return d


def scan_window(today: date, last_scan_at: Optional[datetime],
                config: HawkeyeConfig,
                max_days: Optional[int] = None) -> ScanWindow:
    max_days = max_days if max_days is not None else config.scout_days_back
    end = previous_business_day(today)
    earliest = end - timedelta(days=max_days - 1)
    if last_scan_at is None:
        return ScanWindow(start=earliest, end=end)
    # One day of overlap with the previous run: the earnings calendar
    # back-fills and corrects entries after the fact, so a day read once is
    # worth reading again. Re-reading costs nothing — the (ticker, event
    # date) dedup drops anything already recorded.
    start = last_scan_at.date() - timedelta(days=1)
    truncated = start < earliest
    return ScanWindow(start=min(max(start, earliest), end), end=end,
                      truncated=truncated)


def _catalyst_description(screened: ScreenedEvent) -> str:
    """What the tribunal is told about the surprise, including what we do
    NOT stand behind. A number the screen distrusts must reach the Bull
    labelled, not silently — the prompts instruct both roles to trust
    structured surprise figures over prose, so an unlabelled bad number is
    worse than none at all."""
    parts = [f"EPS surprise {screened.eps_surprise_pct:+.1f}%"
             + ("" if screened.eps_surprise_trusted else
                " [UNVERIFIED: consensus too near zero for a percentage to"
                " carry information]")]
    if screened.revenue_surprise_pct is not None:
        parts.append(f"revenue surprise {screened.revenue_surprise_pct:+.1f}%"
                     + ("" if screened.revenue_surprise_trusted else
                        " [UNVERIFIED: actual and estimate are not on the same"
                        " accounting basis]"))
    if screened.event.numbers_source == "whispers":
        parts.append("both the actual and the consensus above come from the"
                     " earnings feed, not from the calendar; the ratio is"
                     " therefore measured on one vendor's own pair")
    elif screened.event.conflicting_estimates:
        parts.append("the earnings calendar returned conflicting consensus"
                     " figures for this print; the most conservative was used"
                     " and no second source confirmed it")
    return ", ".join(parts) + " (guidance not machine-verified — check news)"


def _candidate_from(screened: ScreenedEvent,
                    reject_reason: str = "") -> ScoutCandidate:
    """A ScoutCandidate carrying both what the screen measured and how far it
    trusts the measurement. Scoring uses only the trusted values."""
    rev = screened.revenue_surprise_pct
    return ScoutCandidate(
        ticker=screened.event.ticker, event_date=screened.event.day,
        eps_surprise_pct=round(screened.eps_surprise_pct, 1),
        revenue_surprise_pct=round(rev, 1) if rev is not None else None,
        score=score_candidate(screened.scored_eps_pct,
                              screened.scored_revenue_pct, None),
        eps_surprise_trusted=screened.eps_surprise_trusted,
        revenue_surprise_trusted=screened.revenue_surprise_trusted,
        conflicting_estimates=screened.event.conflicting_estimates,
        numbers_source=screened.event.numbers_source,
        numbers_reason=screened.event.numbers_reason,
        calendar_eps_surprise_pct=screened.event.calendar_eps_surprise_pct,
        reject_reason=reject_reason)


@dataclass(frozen=True)
class _QuarterContext:
    """The stored context for one print: which company, which quarter, and
    the consensus row the judgment is pinned to."""
    stock_id: str
    print_row: EarningsPrint
    consensus_id: str
    consensus: ConsensusSnapshot


def _quarter_context(store, directory, event,
                     consensus_source=None) -> Optional[_QuarterContext]:
    """Resolve the company and the consensus this print is judged against.

    A pre-registered row always wins. Only when none exists is one
    reconstructed from what the calendar and the earnings feed hold —
    recorded as `reconstructed`, so the weaker evidence is never mistaken
    for the stronger kind later.
    """
    if store is None:
        return None
    stock_id = resolve_stock(store, event.ticker, directory)
    row = print_from_event(event, stock_id)
    as_of = datetime.combine(event.day, time.max, tzinfo=timezone.utc)
    consensus = store.consensus_in_force(stock_id, row.fiscal_quarter,
                                         as_of=as_of)
    if consensus is None:
        snapshot = reconstructed_consensus(event, stock_id, row.fiscal_quarter)
        # The guidance yardstick, when a source is available. It is read
        # AFTER the print, so its "this quarter" row is the quarter now in
        # progress — which is useless as this print's consensus and exactly
        # right as what guidance will be judged against.
        forward = (consensus_source.consensus(event.ticker)
                   if consensus_source is not None else None)
        if forward is not None:
            shifted = shift_after_print(forward)
            snapshot = snapshot.model_copy(update={
                "next_quarter_eps_avg": shifted.next_quarter_eps_avg,
                "next_quarter_revenue_avg": shifted.next_quarter_revenue_avg})
        snapshot_id = store.capture_consensus(snapshot)
        consensus = store.consensus(snapshot_id)
    return _QuarterContext(stock_id=stock_id, print_row=row,
                           consensus_id=consensus.id, consensus=consensus)


def _record_print(store, context: _QuarterContext) -> None:
    """Record a quarter once, and never re-record it.

    Scan windows overlap by design, so the same print arrives again on the
    next run and would be refused by the one-active-row-per-quarter index. A
    repeat carries no new information anyway, so it is skipped here rather
    than allowed to raise.

    A repeat with a DIFFERENT figure is a revision, not a repeat, and it does
    not go through this path: the 48-hour re-fetch compares the two and asks
    the user before anything is retired (task 8.5). Until that exists, a
    corrected actual is simply not picked up by the scan.
    """
    # An unlabelled print is not recorded at all. The active-row index is
    # (company, quarter), so a row with an empty quarter makes "" that
    # company's quarter — and the next print that also fails to get a label
    # then looks like a row already there, so the scan skips it silently.
    # Nothing can join an unlabelled row to its consensus anyway, which is
    # why pre-registration already refuses one (EW移行 §2).
    if not context.print_row.fiscal_quarter:
        return
    existing = store.active_print(context.stock_id,
                                  context.print_row.fiscal_quarter)
    if existing is not None:
        return
    store.record_print(context.print_row.model_copy(
        update={"consensus_snapshot_id": context.consensus_id}))


def _record_cheap_history(store, directory, events,
                          screened_tickers: set[str],
                          skip: Optional[set[tuple[str, date]]] = None) -> int:
    """Give every name that entered the funnel an unbroken quarterly history
    (docs/design/MASTER_OVERVIEW.ja.md §6.1(C)).

    Runs AFTER the enrichment walk, so a quarter the walk already recorded
    keeps the row it wrote — that one may carry a second source's actual,
    where this pass only ever has the calendar's.

    Who is in scope is deliberately narrow. Every name that passed the screen
    is, because it entered the funnel; every name already in the master is,
    because its history is what the master exists for. A name that is neither
    is skipped: importing the whole earnings calendar would leave "stocks we
    follow" meaning nothing. No API call is made — the numbers were in the
    calendar response already.
    """
    if store is None:
        return 0
    held = skip or set()
    written = 0
    for event in events:
        if event.eps_actual is None or (event.ticker, event.day) in held:
            continue
        known = store.stock_by_ticker(event.ticker)
        screened_here = event.ticker in screened_tickers
        if not screened_here and known is None:
            continue
        stock_id = (resolve_stock(store, event.ticker, directory)
                    if screened_here else known.id)
        row = print_from_event(event, stock_id)
        if store.active_print(stock_id, row.fiscal_quarter) is not None:
            continue
        # A pre-registered row always wins; record_print() pins the one in
        # force when none is named here. Only when nothing exists is the
        # calendar's own estimate kept, so the actuals in this row still have
        # something to be judged against later.
        in_force = store.consensus_in_force(stock_id, row.fiscal_quarter)
        snapshot_id = in_force.id if in_force is not None else \
            store.capture_consensus(
                reconstructed_consensus(event, stock_id, row.fiscal_quarter))
        # The source is whichever vendor actually supplied this row's figures,
        # not a blanket label: a name the feed pass happened to cover
        # carries that vendor, and stamping every history row with the
        # calendar would understate what is behind it.
        store.record_print(row.model_copy(
            update={"consensus_snapshot_id": snapshot_id}))
        written += 1
    return written


def _not_worth_a_lookup(store, screened: list[ScreenedEvent], today: date,
                        config) -> set[tuple[str, date]]:
    """Prints whose company the entry gates already refused, structurally.

    Fails OPEN in every uncertain direction, exactly as pre-registration does
    (hawkeye/scout/triage.py): no store, no master row, no verdict, or a
    verdict old enough to be wrong all mean "ask anyway". A wrong exclusion
    here costs the feed's reading of a print that can never be re-read at the
    moment it mattered; a wrong inclusion costs one request.
    """
    if store is None:
        return set()
    return {(s.event.ticker, s.event.day) for s in screened
            if not is_investigation_target(
                store.stock_by_ticker(s.event.ticker), today, config)}


def run_scout(calendar_source, provider: MarketDataProvider, config: HawkeyeConfig,
              days_back: Optional[int] = None,
              today: Optional[date] = None,
              window: Optional[ScanWindow] = None,
              already_seen: Optional[set[tuple[str, date]]] = None,
              numbers_source: Optional[WhispersReader] = None,
              stock_store=None,
              directory=None,
              consensus_source=None) -> ScoutResult:
    """calendar_source: object with earnings_calendar(start, end) -> list[dict]
    provider: MarketDataProvider for enrichment (prices/profile/news).
    window: the earnings days to cover — normally built by scan_window()
        from the previous run. `days_back` is the manual override.
    already_seen: (ticker, event date) pairs an earlier scan already
        recorded, dropped before enrichment so they cost no API calls.
    numbers_source: the earnings feed, read before the shortlist is
        decided, and the vendor whose figures rank the pool when it answers
        (hawkeye/scout/numbers.py). Optional — without it the calendar's own
        figures stand for every name.
    """
    today = today or date.today()
    if window is None:
        end = previous_business_day(today)
        span = days_back if days_back is not None else config.scout_days_back
        window = ScanWindow(start=end - timedelta(days=span - 1), end=end)

    def screen(evts: list) -> list[ScreenedEvent]:
        return screen_events(evts,
                             config.scout_min_eps_surprise_pct,
                             config.scout_min_revenue_surprise_pct,
                             config.scout_min_abs_eps_estimate,
                             config.scout_max_trusted_revenue_surprise_pct)

    # Deduplicate before anything is paid for: windows overlap by design, so
    # the same earnings event recurs across runs. Re-evaluating it would waste
    # free-tier calls and double-count it in the drop statistics. On a 7-day
    # window run daily, six prints in seven arrive again — so this ran in the
    # wrong place for as long as it sat after the second-source pass, which
    # read every one of them and then discarded the reading.
    #
    # There used to be one exemption — a print this system had asked for a
    # release document about was let through even though it was already seen.
    # Nothing asks for documents now, so nothing is exempt. The 48-hour hold
    # for a print whose OWN numbers have not arrived will need an exemption of
    # its own; it is a different condition and gets written with that feature.
    seen = already_seen or set()

    def unseen(rows: list[ScreenedEvent]) -> list[ScreenedEvent]:
        return [s for s in rows if (s.event.ticker, s.event.day) not in seen]

    raw = calendar_source.earnings_calendar(window.start, window.end)
    events = parse_calendar(raw)
    # Screened twice on purpose. The first pass is provisional — it only
    # decides which names are worth a request to the feed — and the second is
    # the one that ranks, because by then the figures are the feed's own.
    # Reading the feed after the ranking would leave
    # the 2026-08-01 defect intact: the broken metric was also the metric
    # choosing which candidates were ever looked at.
    #
    # `all_screened` keeps the pre-dedup list because the reported counts are
    # about the screen ("how many prints cleared the thresholds") while the
    # walk below is about this run's new work.
    all_screened = screen(events)
    screened = unseen(all_screened)
    # Names the entry gates have ALREADY refused on the company's own
    # properties — too small, too cheap, too illiquid — do not get a request
    # (§6.1(E)). The verdict is free: it is a reading of a gate result this
    # funnel recorded on an earlier run, so nothing is fetched to produce it,
    # and it expires (`stock_triage_ttl_days`) because a $3 company can be a
    # $9 company next quarter.
    #
    # It cannot run BEFORE the screen, and that is not an oversight: market
    # cap, price and volume come from a paid per-name call, so gating on them
    # first would mean paying the more expensive call first. This is the cheap
    # cached shadow of that. Its bite grows as the master fills — 2 of 870
    # rows carried a verdict on 2026-08-03, so today it excludes almost
    # nothing, and it costs nothing either.
    events, numbers = read_numbers(events, screened, numbers_source,
                                   config.scout_max_whispers,
                                   skip=seen | _not_worth_a_lookup(
                                       stock_store, screened, today, config))
    # Re-screen whenever the feed was consulted at all, not only when it
    # supplied figures. The first screen ran on events that predate the read,
    # so keeping it would discard everything the read attached even to the
    # names it declined — including WHY it declined, which is what the hold
    # decides on and what the drop record has to carry. Screening is pure
    # computation over a list already in memory, so the reprieve costs
    # nothing and the stale-list class of bug goes away with it.
    if numbers.attempted:
        all_screened = screen(events)
        screened = unseen(all_screened)

    screened_tickers = {s.event.ticker for s in all_screened}
    duplicates = len(all_screened) - len(screened)
    screened_total = len(all_screened)

    # Prints whose OWN numbers have not arrived cannot be ranked, and must not
    # be ranked on the calendar's instead: that would write the print row, the
    # dedup would refuse the print on every later scan, and the feed's reading
    # would never be taken (hawkeye/scout/waiting.py). They are set aside here
    # — before enrichment, so they cost nothing — and recorded as pending so
    # the next scan reads them again.
    held: list[ScoutCandidate] = []
    rankable: list[ScreenedEvent] = []
    for s in screened:
        reason = held_reason(s.event)
        if not reason:
            rankable.append(s)
            continue
        candidate = _candidate_from(s, reject_reason=reason)
        candidate.held_reason = reason
        candidate.held_expired = wait_expired(
            s.event.day, today, config.earnings_actual_wait_hours)
        held.append(candidate)
    screened = rankable
    held_keys = {(c.ticker, c.event_date) for c in held}

    # Walk the ranked screen until enough candidates have PASSED the gates,
    # not until a fixed number have been TRIED. A fixed slice meant a day
    # where 14 of the top 15 failed the gates sent exactly one name to the
    # tribunal while rank 16 sat untouched — the cap was written as an
    # attempt budget but was being read as a shortlist (found 2026-08-02).
    # Both bounds are still needed: the target says when there is enough to
    # rank among, the ceiling says when to stop paying for a bad day.
    passed: list[ScoutCandidate] = []
    rejected: list[ScoutCandidate] = []
    attempted = 0
    stopped_at = len(screened)

    for position, s in enumerate(screened):
        if (len(passed) >= config.scout_target_gate_passed
                or attempted >= config.scout_max_enrich):
            stopped_at = position
            break
        attempted += 1
        event = s.event
        candidate = _candidate_from(s)   # score refined below once gap is known
        # The three-leg reading, when a store is available. Computed before
        # the brief because it decides what the tribunal is told AND which
        # numbers are allowed to become structured facts; the event-day
        # reaction only refines the score afterwards.
        context = _quarter_context(stock_store, directory, event,
                                   consensus_source)
        quality = (assess_earnings(context.print_row, context.consensus, config)
                   if context is not None else None)
        catalyst = Catalyst(
            type=CatalystType.EARNINGS_BEAT,
            description=(describe_quality_en(quality) if quality is not None
                         else _catalyst_description(s)),
            event_date=event.day, source="scout/finnhub-earnings-calendar")
        if context is not None:
            _record_print(stock_store, context)
        try:
            # Only trusted figures become structured snapshot fields: the
            # tribunal prompts tell both roles to prefer these over prose, so
            # a distrusted number here would be laundered into a fact. The
            # catalyst description still says what was measured, and why it
            # is not stood behind.
            eps_fact = (quality.eps.scored_pct if quality is not None
                        else s.scored_eps_pct)
            revenue_fact = (quality.revenue.scored_pct if quality is not None
                            else s.scored_revenue_pct)
            brief = build_brief(
                candidate.ticker, catalyst, provider,
                overrides={
                    "eps_surprise_pct": (round(eps_fact, 1)
                                         if eps_fact is not None else None),
                    "revenue_surprise_pct": (
                        round(revenue_fact, 1)
                        if revenue_fact is not None else None)},
                config=config)
        except Exception as exc:  # enrichment failure = rejection, visibly
            candidate.reject_reason = f"enrichment failed: {exc}"
            rejected.append(candidate)
            continue
        candidate.brief = brief
        candidate.price = brief.snapshot.price
        candidate.price_asof = utc_date(brief.snapshot.as_of)
        if quality is not None:
            quality = assess_earnings(context.print_row, context.consensus,
                                      config, brief.snapshot.gap_on_event_pct)
            candidate.quality = quality
            candidate.score = quality.score
            candidate.score_version = "three_leg"
        else:
            candidate.score = score_candidate(
                s.scored_eps_pct, s.scored_revenue_pct,
                brief.snapshot.gap_on_event_pct)
            candidate.score_version = "full"
        candidate.gate_report = run_entry_gates(brief.snapshot, catalyst,
                                                config, today=today)
        # Free by-product: the gates just measured price, size and liquidity,
        # so the master can record whether this company could ever hold a
        # position — which is what keeps tomorrow's pre-registration from
        # spending a lookup on it (§6.1(E)). Only the structural gates count,
        # and only when they were actually verified.
        verdict = triage_from_gates(candidate.gate_report)
        if verdict is not None and context is not None:
            stock_store.record_triage(context.stock_id, verdict.is_target,
                                      verdict.reason, on=today)
        if not candidate.gate_report.ok:
            candidate.reject_reason = "gate: " + ", ".join(
                g.name for g in candidate.gate_report.hard_failures)
            rejected.append(candidate)
            continue
        passed.append(candidate)

    passed.sort(key=lambda c: -c.score)

    # #2 in docs/design/MASTER_OVERVIEW.ja.md §5.1: candidates below where the walk
    # stopped never get a full brief — one cheap price-only fetch each (not
    # the full multi-call enrichment) so they're still trackable, instead of
    # vanishing with no record at all. Which of the two bounds stopped the
    # walk is recorded: "the pool filled up" and "the budget ran out" are
    # different facts about the same run.
    ceiling_hit = attempted >= config.scout_max_enrich and len(
        passed) < config.scout_target_gate_passed
    why_capped = ("enrichment ceiling: scout_max_enrich attempts spent before"
                  f" {config.scout_target_gate_passed} candidates passed the"
                  " gates" if ceiling_hit else
                  "enrichment cap: the gate-passed pool filled up above this"
                  " candidate's score")
    capped: list[ScoutCandidate] = []
    for s in screened[stopped_at:]:
        c = _candidate_from(s, reject_reason=why_capped)
        try:
            bars = provider.daily_history(s.event.ticker, days=5)
            if bars:
                c.price, c.price_asof = bars[-1].close, bars[-1].day
        except Exception:
            pass
        capped.append(c)

    # Last, so a quarter that earned a deeper reading above keeps it. Held
    # prints are excluded: a history row IS a row for that quarter, so writing
    # one would make the next scan skip the print as already recorded and the
    # hold would quietly end in the calendar's numbers after all.
    _record_cheap_history(stock_store, directory, events, screened_tickers,
                          skip=held_keys)

    return ScoutResult(scan_start=window.start, scan_end=window.end,
                       scanned=len(raw), screened=screened_total,
                       enriched=attempted,
                       passed=passed, rejected=rejected, capped=capped,
                       held=held, duplicates=duplicates,
                       window_truncated=window.truncated,
                       numbers=numbers,
                       enrichment_ceiling_hit=ceiling_hit)


def _visible_at_drop(c: ScoutCandidate) -> dict:
    """The qualitative data enrichment already fetched for this candidate.

    Empty when the candidate never got a brief — dropped at the enrichment
    cap, or enrichment itself failed. Absence is therefore "never looked",
    which is a different fact from "looked and found nothing"; the stage
    field is what distinguishes them (docs/design/MASTER_OVERVIEW.ja.md §5.2(5)).
    """
    if c.brief is None:
        return {}
    return {"news": c.brief.news,
            "insider_activity": c.brief.insider_activity,
            "analyst_trend": c.brief.analyst_trend}


def _measured(c: ScoutCandidate) -> dict:
    """What the screen measured and how far it trusted it — the fields every
    dropped-candidate record carries regardless of which stage dropped it."""
    return {"eps_surprise_pct": c.eps_surprise_pct,
            "revenue_surprise_pct": c.revenue_surprise_pct,
            "eps_surprise_trusted": c.eps_surprise_trusted,
            "revenue_surprise_trusted": c.revenue_surprise_trusted,
            "conflicting_estimates": c.conflicting_estimates,
            "numbers_source": c.numbers_source,
            "numbers_reason": c.numbers_reason,
            "calendar_eps_surprise_pct": c.calendar_eps_surprise_pct,
            "score": c.score, "score_version": c.score_version,
            "price": c.price, "price_asof": c.price_asof}


def build_screened_candidates(
    result: ScoutResult, scan_id: int, sent_to_tribunal_n: int = 0,
) -> list[ScreenedCandidate]:
    """Convert everything run_scout() dropped (docs/design/MASTER_OVERVIEW.ja.md
    §5.1, stages #2-#4) into persistable records for
    Ledger.record_screened_candidates(). `sent_to_tribunal_n` is the one
    piece run_scout() itself can't know — how many of `result.passed` this
    particular run forwarded on (the caller's --evaluate/--open-cases
    choice) — so the ranking-cutoff tier (#4) is computed here, by the
    caller, immediately after that choice is made.

    Every candidate here except a HELD one is being dropped for the first
    time: the dedup refuses a print any earlier scan already recorded. Held
    prints are the deliberate exemption — one `actual_pending` row per scan is
    how "we looked again and the numbers still were not there" is recorded, so
    the same print appears once per look. The drop review excludes that stage
    from its tallies for exactly this reason; a name that was never judged
    must not count toward the 20-sample bar that decides whether the screen
    gets revised.
    """
    out: list[ScreenedCandidate] = []
    for c in result.held:
        out.append(ScreenedCandidate(
            scan_id=scan_id, ticker=c.ticker, event_date=c.event_date,
            stage=(ScreenedCandidateStage.ACTUAL_TIMEOUT if c.held_expired
                   else ScreenedCandidateStage.ACTUAL_PENDING),
            reject_reason=c.reject_reason,
            **_measured(c), **_visible_at_drop(c)))
    for c in result.capped:
        out.append(ScreenedCandidate(
            scan_id=scan_id, ticker=c.ticker, event_date=c.event_date,
            stage=ScreenedCandidateStage.ENRICHMENT_CAP,
            reject_reason=c.reject_reason,
            **_measured(c), **_visible_at_drop(c)))
    for c in result.rejected:
        out.append(ScreenedCandidate(
            scan_id=scan_id, ticker=c.ticker, event_date=c.event_date,
            stage=ScreenedCandidateStage.GATE_REJECT,
            gate_report=c.gate_report, reject_reason=c.reject_reason,
            **_measured(c), **_visible_at_drop(c)))
    for i, c in enumerate(result.passed[sent_to_tribunal_n:],
                          start=sent_to_tribunal_n + 1):
        out.append(ScreenedCandidate(
            scan_id=scan_id, ticker=c.ticker, event_date=c.event_date,
            stage=ScreenedCandidateStage.RANKING_CUTOFF, rank=i,
            gate_report=c.gate_report,
            reject_reason=f"rank {i}, outside this run's top {sent_to_tribunal_n}",
            **_measured(c), **_visible_at_drop(c)))
    return out
