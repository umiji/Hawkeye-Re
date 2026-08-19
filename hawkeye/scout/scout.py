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

import sys
from dataclasses import dataclass, field, replace
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
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
)
from hawkeye.gates.entry_gates import run_entry_gates
from hawkeye.marketdata.base import MarketDataProvider
from hawkeye.marketdata.snapshot import build_brief
from hawkeye.scout.earnings import (
    ScreenedEvent,
    parse_calendar,
    score_candidate,      # re-exported: the ranking score lives with the screen
    screen_events,
)
from hawkeye.scout import cause_case, guidance_case
from hawkeye.scout.guidance_agent import GuidanceStats
from hawkeye.scout.inspection import Inspection, build_inspection, was_asked
from hawkeye.scout.prereg import resolve_stock
from hawkeye.scout.revision import Revision, apply_revision, detect_revisions
from hawkeye.scout.triage import is_investigation_target, triage_from_gates
from hawkeye.scout.quality import (
    EarningsQuality,
    assess_earnings,
    describe_quality_en,
    guidance_comparisons,
    guidance_state,
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
    # How the company's own release was cut into the cause excerpt, carried
    # here so the scan report the USER reads can show it per ticker (T-013).
    # All four zero means the release was never read for this name — dropped
    # before enrichment, or no extractor key — which is why the report says
    # "not read" there rather than "refused 0".
    cause_blocks_kept: int = 0
    cause_blocks_repaired: int = 0
    cause_blocks_altered: int = 0
    cause_blocks_refused: int = 0
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
    # Carried alongside `quality` so a later ranking pass can recompute the
    # score once guidance has actually been read (docs/design/RANK_AFTER_GUIDANCE.ja.md).
    # `consensus` has to travel as the object itself, not a snapshot id: the
    # guidance yardsticks are overlaid on it IN MEMORY by `_quarter_context`
    # and are never written back to the store, so re-fetching by id would
    # silently drop them.
    stock_id: str = ""
    consensus: Optional[ConsensusSnapshot] = None


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
    # Figures a vendor restated after we had already recorded them (task 8.5).
    # Reported ABOVE the shortlist, because a correction the reader meets after
    # they have read the ranking is a correction they will not act on.
    revisions: list[Revision] = field(default_factory=list)
    # What the guidance extraction step did (task 8.7 layer 2). Zero
    # everywhere when no reader was supplied — which is a different fact from
    # "it ran and found nothing", and the two must not print the same.
    guidance: GuidanceStats = field(default_factory=GuidanceStats)
    # The check sheet: one row per name the earnings feed was asked about
    # (hawkeye/scout/inspection.py). Not a judgment — it exists so the reader
    # can verify the data behind the ranking before reading the ranking.
    inspection: Inspection = field(default_factory=Inspection)

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


def _forward_bars(event) -> dict:
    """The yardsticks guidance is judged against, as this print's own summary
    states them. Empty entries are dropped so an overlay never blanks a figure
    a stored row already carries."""
    return {key: value for key, value in (
        ("next_quarter_eps_avg", event.next_quarter_eps_estimate),
        ("next_quarter_revenue_avg", event.next_quarter_revenue_estimate),
        ("full_year_eps_avg", event.full_year_eps_estimate),
        ("full_year_revenue_avg", event.full_year_revenue_estimate),
        ("full_year_period", event.full_year_period)) if value}


def _quarter_context(store, directory, event) -> Optional[_QuarterContext]:
    """Resolve the company and the consensus this print is judged against.

    A pre-registered row always wins for the SURPRISE RATIO — that is the
    whole point of capturing early. Only when none exists is one reconstructed
    from what the calendar and the earnings feed hold, recorded as
    `reconstructed` so the weaker evidence is never mistaken for the stronger
    kind later.

    The guidance yardsticks are the exception, and they have to be: they exist
    only in the summary this print carried, so no row captured BEFORE the
    print can hold one. Judging on the pre-registered row alone therefore
    recorded every pre-registered name as having guided nothing. They are
    overlaid IN MEMORY — the stored row is never rewritten, because "this is
    what was expected beforehand" is the one claim it makes.

    What that costs: for a pre-registered name the bar the guidance leg used
    is not itself written to the ledger. It is re-derivable from the print's
    summary, but it is not a stored figure the way a reconstruction's is.
    """
    if store is None:
        return None
    stock_id = resolve_stock(store, event.ticker, directory)
    row = print_from_event(event, stock_id)
    as_of = datetime.combine(event.day, time.max, tzinfo=timezone.utc)
    consensus = store.consensus_in_force(stock_id, row.fiscal_quarter,
                                         as_of=as_of)
    if consensus is None:
        snapshot_id = store.capture_consensus(
            reconstructed_consensus(event, stock_id, row.fiscal_quarter))
        consensus = store.consensus(snapshot_id)
    else:
        bars = _forward_bars(event)
        if bars:
            consensus = consensus.model_copy(update=bars)
    return _QuarterContext(stock_id=stock_id, print_row=row,
                           consensus_id=consensus.id, consensus=consensus)


def _stage_cause(context: _QuarterContext, event,
                 cause_source) -> tuple[str, dict]:
    """Stage the cause reading, and return the reason it could not be, plus
    how the release was cut.

    Split from the guidance staging beside it because the two no longer read
    the same text (T-008). Guidance is in the vendor's summary; the reason
    the quarter came out where it did is not — 0 of 30 prints yielded one
    (measured 2026-08-17) — and comes from the company's own release, fetched
    and cut here.

    With no `cause_source` wired the old behaviour stands exactly: the
    summary is staged as before. That path is what every offline test and
    every scan without an extractor key still takes, and it must keep
    yielding the same rows — and it returns no counts at all, which is how
    "the release was never read" stays distinguishable from "it was read and
    nothing survived" (T-013).
    """
    if cause_source is None or not getattr(cause_source, "available", False):
        cause_case.save_case(cause_case.CauseCase(
            stock_id=context.stock_id, print_id=context.print_row.id,
            ticker=event.ticker,
            fiscal_quarter=context.print_row.fiscal_quarter,
            summary=event.summary))
        return "pending_extraction", {}

    built = cause_source.text_for(event.ticker,
                                  getattr(event, "article_id", ""),
                                  context.print_row.fiscal_quarter)
    counts = {"cause_blocks_kept": built.kept,
              "cause_blocks_repaired": len(built.repaired),
              "cause_blocks_altered": len(built.altered),
              "cause_blocks_refused": len(built.rejected)}
    if built.altered:
        # Said while the scan is running, because it is the one thing here
        # that no later column explains on its own: the excerpt is CORRECT
        # (the release's characters were used), so without this line an
        # extractor quietly rewriting figures looks exactly like a clean run.
        for sent, actual in built.altered:
            print(f"  {event.ticker}: the extractor altered the company's "
                  f"words — sent {sent[-60:]!r}, release says "
                  f"{actual[-60:]!r}", file=sys.stderr)
    if not built.excerpt:
        # Nothing to read. WHICH nothing it is has already been decided by
        # the source and must survive to the row: "no release reached us",
        # "the release explains nothing" and "our extractor composed every
        # block" are three different facts and only the last is ours to fix.
        if built.detail:
            # The row keeps the classifiable reason; the operator gets the
            # sentence behind it. A scan that quietly drops a third of its
            # names to a spent quota should say so while it is running, not
            # leave it to be inferred from a column a day later (T-011).
            print(f"  {event.ticker}: {built.reason} — {built.detail}",
                  file=sys.stderr)
        return built.reason, counts
    cause_case.save_case(cause_case.CauseCase(
        stock_id=context.stock_id, print_id=context.print_row.id,
        ticker=event.ticker,
        fiscal_quarter=context.print_row.fiscal_quarter,
        summary=built.excerpt, source_text=built.source_text))
    return "pending_extraction", counts


def _stage_prose_reads(store, context: _QuarterContext, event,
                       stats: GuidanceStats,
                       cause_source=None) -> _QuarterContext:
    """Stage the two readings only an agent can make, or say why there is
    nothing to stage.

    Both come out of the same paragraph of vendor prose and neither exists as
    a number anywhere: what the company expects NEXT quarter, and what it said
    explains the quarter it just reported (T-003). `stats` counts the first
    only — the second changes no score, so nothing waits on it, and giving it
    a second counter in the scan report would imply the run was blocked on it.

    Runs BEFORE the row is recorded, so the row is written once. Nothing
    inside a scan process can call an agent itself — the scan only ever
    writes the sentence to `var/guidance/`; `hawkeye guidance queue` /
    `submit` close the loop afterwards, and `hawkeye rank`
    (docs/design/RANK_AFTER_GUIDANCE.ja.md) is what re-scores the print once
    that reading has landed. The row says the reading is OUTSTANDING rather
    than saying the company guided nothing — the two render identically and
    mean opposite things.

    A print with no summary is skipped: the feed declined that name
    entirely, so no sentence exists and the extraction step never had a turn.
    """
    if not event.summary:
        # No sentence to read, and WHY there is none is a fact about us, not
        # about the company. Left unrecorded (as it was until 2026-08-11) the
        # empty reason falls through to `guidance_not_published` in
        # `_guidance_leg` — so a feed outage was written down as "this company
        # publishes no outlook", permanently, in the drop record a later
        # review reads. Same conflation the 未読/開示なし split fixed on
        # 2026-08-10, one layer down.
        missing = ("no_summary_from_feed" if was_asked(event)
                   else "feed_not_asked")
        return replace(context, print_row=context.print_row.model_copy(
            update={"guidance_reason": missing, "cause_reason": missing}))
    # Nothing is staged for a quarter already on record: `_record_print`
    # will skip it as a repeat, so the case would point at a row this scan
    # never wrote.
    if store.active_print(context.stock_id,
                          context.print_row.fiscal_quarter) is None:
        guidance_case.save_case(guidance_case.GuidanceCase(
            stock_id=context.stock_id, print_id=context.print_row.id,
            ticker=event.ticker,
            fiscal_quarter=context.print_row.fiscal_quarter,
            summary=event.summary))
        stats.staged += 1
        # A second queue for a different question: that one asks what the
        # company expects next quarter, this one asks what it said about the
        # quarter just reported (T-003). Two agents rather than one because
        # an extractor with two jobs can satisfy the easier one and call it
        # an answer, and because each is checked against a different set of
        # decoy sentences.
        #
        # No longer the same sentence, since T-008: guidance IS in the
        # vendor's summary and the reason for the quarter is not, so this
        # queue is fed from the company's own release instead.
        cause_reason, cause_counts = _stage_cause(context, event,
                                                  cause_source)
    else:
        cause_reason, cause_counts = "pending_extraction", {}
    return replace(context, print_row=context.print_row.model_copy(
        update={"guidance_reason": "pending_extraction",
                "cause_reason": cause_reason, **cause_counts}))


def _record_print(store, context: _QuarterContext, config=None,
                  now=None) -> list[Revision]:
    """Record a quarter once, and never re-record it.

    Scan windows overlap by design, so the same print arrives again on the
    next run and would be refused by the one-active-row-per-quarter index. A
    repeat carries no new information anyway, so it is skipped here rather
    than allowed to raise.

    A repeat with a DIFFERENT figure is a revision, not a repeat: the stored
    row is retired, the corrected one appended, and what moved is returned so
    the run can show the reader before they read the shortlist (task 8.5).
    Only inside the watch window — see `hawkeye/scout/revision.py`.

    Returns the revisions recorded, empty when nothing moved.
    """
    # An unlabelled print is not recorded at all. The active-row index is
    # (company, quarter), so a row with an empty quarter makes "" that
    # company's quarter — and the next print that also fails to get a label
    # then looks like a row already there, so the scan skips it silently.
    # Nothing can join an unlabelled row to its consensus anyway, which is
    # why pre-registration already refuses one (EW移行 §2).
    if not context.print_row.fiscal_quarter:
        return []
    row = context.print_row.model_copy(
        update={"consensus_snapshot_id": context.consensus_id})
    existing = store.active_print(context.stock_id,
                                  context.print_row.fiscal_quarter)
    if existing is None:
        store.record_print(row)
        return []
    if config is None:
        return []
    revisions = detect_revisions(existing, row, config, now=now)
    if revisions:
        apply_revision(store, row)
    return revisions


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
              cause_source=None) -> ScoutResult:
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
    cause_source: reads the company's OWN earnings release and cuts it to
        the blocks explaining the quarter (T-008,
        hawkeye/scout/cause_source.py). Optional — without it the cause
        queue is fed the vendor's summary as before, which is the text that
        explained 0 of 30 prints.
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
    revisions: list[Revision] = []
    guidance_stats = GuidanceStats()
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
        context = _quarter_context(stock_store, directory, event)
        # The third leg, read by an agent out of the same prose that carried
        # the numbers (task 8.7 layer 2). Before the judgment, because it IS
        # one of the three things being judged.
        if context is not None:
            context = _stage_prose_reads(stock_store, context, event,
                                         guidance_stats, cause_source)
            # Carried onto the candidate as well as the print row, because
            # the two are read by different people: the row is the permanent
            # record, and the candidate is what the user's scan report is
            # built from (T-013).
            for field in ("cause_blocks_kept", "cause_blocks_repaired",
                          "cause_blocks_altered", "cause_blocks_refused"):
                setattr(candidate, field, getattr(context.print_row, field))
        quality = (assess_earnings(context.print_row, context.consensus, config)
                   if context is not None else None)
        catalyst = Catalyst(
            type=CatalystType.EARNINGS_BEAT,
            description=(describe_quality_en(quality) if quality is not None
                         else _catalyst_description(s)),
            event_date=event.day, source="scout/finnhub-earnings-calendar")
        if context is not None:
            revisions.extend(_record_print(stock_store, context, config))
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
            candidate.stock_id = context.stock_id
            candidate.consensus = context.consensus
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

    result = ScoutResult(scan_start=window.start, scan_end=window.end,
                         scanned=len(raw), screened=screened_total,
                         enriched=attempted,
                         passed=passed, rejected=rejected, capped=capped,
                         held=held, duplicates=duplicates,
                         window_truncated=window.truncated,
                         numbers=numbers,
                         enrichment_ceiling_hit=ceiling_hit,
                         revisions=revisions,
                         guidance=guidance_stats)
    # The check sheet (§10), built here because this is the only place that
    # still holds both halves: the events carry what each vendor said, and the
    # candidate lists carry how far the walk took each name. Assembled after
    # the result so the stage of every name is already decided.
    result.inspection = build_inspection(events, result,
                                         revisions_seen=len(revisions))
    return result


def rerank_after_guidance(store, result: ScoutResult, config: HawkeyeConfig
                          ) -> ScoutResult:
    """Re-score every judged candidate once the guidance queue is empty, and
    re-sort the shortlist on the result (docs/design/RANK_AFTER_GUIDANCE.ja.md).

    `run_scout` judges a quarter the moment it walks past it, which is BEFORE
    session mode's `hawkeye guidance queue` / `submit` can have supplied the
    company's own outlook — so the guidance leg the scan scored is always
    "not yet read", never the real answer. Ranking on that score means the
    shortlist — and the ledger rows recorded from it — are decided on
    incomplete data.

    This is the one place that reads the store again after the walk: the
    active print row for a judged candidate's (stock, quarter) now carries
    whatever `hawkeye guidance submit` attached, if anything did. The
    consensus travels on the candidate itself rather than being re-fetched,
    because the guidance yardsticks `_quarter_context` overlaid onto it exist
    only in that in-memory copy (see `ScoutCandidate.consensus`).

    Mutates `result` in place and returns it, so a caller can chain this onto
    a freshly loaded scan without juggling two names for the same object.
    """
    for bucket in (result.passed, result.rejected):
        for candidate in bucket:
            if candidate.quality is None:
                continue
            fresh_print = store.active_print(candidate.stock_id,
                                             candidate.quality.fiscal_quarter)
            if fresh_print is None:
                continue
            gap = (candidate.brief.snapshot.gap_on_event_pct
                   if candidate.brief is not None else None)
            candidate.quality = assess_earnings(
                fresh_print, candidate.consensus, config, gap)
            candidate.score = candidate.quality.score
            # The score is only half of what the reading changes. No role ever
            # sees the verdict above — the Bull, the Adversary and the Judge
            # see one English paragraph on the brief, and `run_scout` wrote it
            # while this leg still read "not yet read", because in session mode
            # nothing had read it yet. Left alone, every role argues from a
            # dossier saying the company disclosed no outlook however clearly
            # it did, while the score beside it says otherwise (T-005).
            #
            # ONLY the paragraph. The structured figures next to it were fixed
            # when the scan stood behind them, and the prompts tell both roles
            # to prefer those over prose — moving one here would launder a
            # second reading into a fact.
            if candidate.brief is not None:
                candidate.brief.catalyst = candidate.brief.catalyst.model_copy(
                    update={"description":
                            describe_quality_en(candidate.quality)})
    result.passed.sort(key=lambda c: -c.score)
    return result


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
            # The figures the percentages above were computed from, exactly as
            # the ranking read them (T-014). Taken off the three-leg reading
            # rather than re-derived, so the table the user approves shows the
            # numbers the ranking was actually made on. All None when the
            # funnel ran without a stock store — the reading never happened.
            "eps_actual": c.quality.eps.actual if c.quality else None,
            "eps_estimate": c.quality.eps.estimate if c.quality else None,
            "revenue_actual": c.quality.revenue.actual if c.quality else None,
            "revenue_estimate": (c.quality.revenue.estimate if c.quality
                                 else None),
            # Whether the company's outlook was obtained, and the named reason
            # when it was not (T-014).
            "guidance_state": guidance_state(c.quality),
            "guidance_reason": c.quality.guidance_reason if c.quality else "",
            # The figures the guidance score was computed from, per period and
            # per unit (T-018) — kept beside the score for the same reason the
            # EPS pair above is kept beside its percentage.
            "guidance_comparisons": guidance_comparisons(c.quality),
            "score": c.score, "score_version": c.score_version,
            # What earned the score. None when the funnel ran without a stock
            # store, i.e. when the three-leg reading never happened — the
            # score still exists, but nothing computed a derivation for it,
            # and the report must say that rather than print five zeros.
            "score_breakdown": (c.quality.breakdown if c.quality is not None
                                else None),
            # How the company's own release was cut for this name (T-013).
            # Recorded on every dropped candidate, not only the ranked ones,
            # because "which names do we keep failing to read, and is the
            # reason ours or the extractor's" is a question about the whole
            # scan and cannot be answered from the top three.
            "cause_blocks_kept": c.cause_blocks_kept,
            "cause_blocks_repaired": c.cause_blocks_repaired,
            "cause_blocks_altered": c.cause_blocks_altered,
            "cause_blocks_refused": c.cause_blocks_refused,
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
