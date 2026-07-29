# Architecture Review Backlog (2026-07-28)

Source: a 4-agent parallel review (doc-vs-code consistency, architecture
soundness, python code quality, adversarial investment-methodology audit)
run against the whole `hawkeye/` codebase and `docs/ARCHITECTURE.md` /
`docs/MASTER_OVERVIEW.ja.md`. This is the first and only place the full
finding list is written down — the review conversation itself has since
ended and a separate follow-up session confirmed the raw agent output is
not recoverable except from this file or by re-running the review. Do not
delete this file even after items are triaged; replace "OPEN" with "FIXED
(commit X)" or "WON'T FIX (reason)" instead, so the audit trail survives.

Three items were already fixed same-day as CRITICAL (judge rule-check
substring matching, entry-gate unverified-silent-pass, benchmark cohort
contamination/survivorship bias) — see `CLAUDE.md` hand-off log entries
**2026-07-28** and **2026-07-28(b)**, and
`~/.claude/projects/.../memory/review_2026-07-28_critical_findings.md`.
Those are not repeated here except where a finding below overlaps and is
now only *partially* addressed.

## Status legend

- **OPEN** — not yet acted on, still true as of 2026-07-28.
- **RULED: ACCEPTED LIMITATION** — taken to the user, who explicitly chose
  not to fix it. Do not implement a fix without a new instruction; the
  reasoning is recorded in `CLAUDE.md` and should be read before
  reconsidering.
- **FIXED** — resolved same day, listed here only because it was originally
  part of a compound finding and the fix only covers part of it.

---

## HIGH severity

### H1. Judge/Adversary still receive un-parsed, un-clamped raw LLM JSON for the thesis (FIXED — commit 472421e)

**Source:** architect agent, F4 (part 2/2 — part 1 was the attack-report
side, fixed same day).
**Where:** `hawkeye/tribunal/pipeline.py` — `render_adversary_input`/
`render_judge_input` are called with `thesis_raw` (the Bull's raw dict),
never with the parsed-and-clamped `Thesis` model.
`hawkeye/tribunal/prompts.py:288` puts `thesis_raw` into
`payload["thesis_under_attack"]` verbatim.

**Failure scenario:** `parse_thesis()` renormalizes scenario probabilities
that don't sum to 1.0 and clamps out-of-range values before they reach the
ledger, but the Adversary and Judge argue over the *un-normalized* numbers
the Bull actually wrote. The record that gets debated and the record that
gets stored disagree — reproducing "why did the Judge say this?" from the
ledger later won't match what's in `Recommendation.thesis`. Existing test
`tests/test_pipeline.py::test_scenario_probabilities_normalized` documents
this exact mismatch without treating it as a bug.

**Suggested fix:** same pattern already used for the attack-report fix —
parse once, render `thesis.model_dump(mode="json")` to Adversary/Judge
instead of `thesis_raw`. One-line change per call site once `Thesis` is
parsed before rendering (it already is, just not reused for rendering).

### H2. `converted_to_kill_criterion=true` from the Judge never becomes an actual monitored `KillCriterion` (OPEN — remainder of the Finding-1 compound bug)

**Source:** architect agent, F5 (part 2/2 — part 1, the id-vs-text
matching, was fixed same day as Finding 1).
**Where:** `hawkeye/sentinel/monitor.py:53-76` (`check_position`) only
reads `thesis.kill_criteria`; it never looks at
`verdict.addressed[].converted_to_kill_criterion`.

**Failure scenario:** the Judge can accept a severe risk as "we'll monitor
it" (satisfying the rule-check, correctly now via `attack_id`), but nothing
adds that risk to the list `hawkeye check` actually watches. The "accepted
risk with a monitoring plan" is a sentence in the ledger, not a live check
— principle 2 ("execute the stop with zero emotion") has a hole exactly
where the Judge exercised judgment instead of an outright refutation.

**Suggested fix:** either (a) require the Judge's schema to emit a real
`KillCriterion`-shaped object when `converted_to_kill_criterion=true`, and
append it to `thesis.kill_criteria` in `assemble_recommendation`, or (b) add
a second rule-check that fails BUY if any `converted_to_kill_criterion=true`
attack has no corresponding kill criterion. (a) is more useful; (b) is
cheaper and catches drift if (a) is ever bypassed.

### H3. `hawkeye check` aborts entirely if one held ticker's price fetch fails (OPEN)

**Source:** architect agent, F7.
**Where:** `hawkeye/cli.py:451` — `bars = provider.daily_history(rec.ticker, days=5)`
with no `try`/`except`, inside the loop over open positions. Compare
`cli.py:276-278` (`cmd_benchmark`), which does guard this.

**Failure scenario:** holding 3 positions, the 2nd ticker's Yahoo request
hits a transient rate limit (`raise_for_status()` in
`hawkeye/marketdata/yahoo.py:31`) → the whole command crashes → the 1st
position's signal was already computed and is lost (nothing printed), the
3rd position's stop-loss check never runs at all. The core "execute stops
with zero emotion" guarantee silently stops running for the rest of that
day's positions, and the failure is indistinguishable from "nothing to
report" unless the user reads a stack trace.

**Suggested fix:** wrap the per-ticker fetch/check in `try`/`except`,
emit an explicit "⚠️ could not verify — check manually" signal instead of
crashing the whole command.

### H4. Scout enrichment failures are indistinguishable from "no candidates today" (OPEN)

**Source:** architect agent, F8; overlapping python-reviewer MEDIUM finding
on the same code with a narrower fix.
**Where:** `hawkeye/scout/scout.py:112-121` — `except Exception as exc:
candidate.reject_reason = f"enrichment failed: {exc}"`, and the funnel
count logged (`enriched=len(to_enrich)`, `cli.py:226-231`) is the *attempt*
count, not the success count.

**Failure scenario:** if Yahoo rate-limits partway through 15 enrichment
calls, all remaining candidates get `reject_reason="enrichment failed"`
and the funnel looks identical to a genuinely quiet earnings day — the
Phase 0 denominator (how many candidates were actually screened) becomes a
function of API reliability, not market activity, with no visible signal
that anything went wrong.

**Suggested fix:** distinguish data-availability exceptions (network/HTTP)
from unexpected ones (let those propagate — python-reviewer's point: a
real bug in `build_brief` shouldn't be silently absorbed as "no data
today" either). Record a separate `enrichment_failed` count in `scans`
distinct from `screened`/`gate_passed`.

### H5. `hawkeye benchmark --horizon` is unpinned and can be re-run at different values with no record of what was tried (OPEN)

**Source:** methodology-auditor.
**Where:** `hawkeye/cli.py` — `bm.add_argument("--horizon", type=int, default=30, ...)`.

**Failure scenario:** `docs/MASTER_OVERVIEW.ja.md` §5.1 explicitly names
this exact trap ("観測期間を記録開始前に決めて書き残す") as a required
safeguard for the *proposed* missed-candidate feature — but the
*already-shipped* `benchmark`/`review-passes` commands have the identical
exposure today, with zero safeguard. Nothing stops re-running at a
different horizon until the spread looks favorable and reporting only that
run.

**Suggested fix:** pin the one official horizon used for the Phase-0
kill-criterion determination (config value with a dated commit, not a free
CLI flag for that specific purpose); label any other horizon's output as
exploratory/non-authoritative.

### H6. No minimum-n or significance treatment on the Phase-0 kill criterion itself (OPEN)

**Source:** methodology-auditor.
**Where:** `docs/ROADMAP.md`'s exit criteria; `hawkeye/scout/benchmark.py::cohort_stats`.

**Failure scenario:** the funnel-discrimination exit criterion itself
admits BUY rate could be 10-40% of evaluated candidates — at the low end,
50 evaluated candidates could yield single-digit BUY n, and 30-day
single-stock return volatility is large enough that a raw mean-vs-mean
comparison at n≈5-10 is dominated by noise. A comfortable-looking spread at
that point would look like evidence without being evidence.

**Suggested fix:** specify a minimum BUY-cohort n (separate from "≥50
evaluated") and a significance/CI bar before the Phase-0 spread is treated
as decision-grade — the project's own Phase-3 exit criteria already use a
bootstrap CI elsewhere; reuse that machinery here.

---

## MEDIUM severity

### M1. `reports/render_ja.py` imports sentinel's internal dataclass directly, not a contract model (OPEN)

**Source:** architect agent, F1.
**Where:** `hawkeye/reports/render_ja.py:13` — `from hawkeye.sentinel.monitor import Signal`.
`Signal` is a frozen dataclass local to `sentinel/monitor.py`, not in
`hawkeye/contracts/models.py`.

**Failure scenario:** breaks the stated design constraint ("packages
communicate only through `hawkeye.contracts`"). If `reports` is ever split
into a standalone notification service, or `Signal.severity` changes from
`str` to an enum, this import breaks silently at the type level (no
contract boundary catches it).

**Suggested fix:** move `Signal` into `hawkeye/contracts/models.py` as a
pydantic model; `sentinel` has no other state, so the move is close to
free.

### M2. `render_scout_ja` depends on scout's internal dataclasses with no type annotation (OPEN)

**Source:** architect agent, F2.
**Where:** `hawkeye/reports/render_ja.py:178-215` reads `.scan_start`,
`.funnel()`, `.passed[].brief`, `.score` off `ScoutResult`/`ScoutCandidate`
(`hawkeye/scout/scout.py:31-55`), neither of which is a contract model, and
the function signature has no type hint naming them.

**Failure scenario:** same class of risk as M1 — a field rename in
`scout.py` won't be caught by a type checker, only at runtime.

**Suggested fix:** add explicit type hints to `render_scout_ja`'s
signature at minimum; consider promoting `ScoutResult`/`ScoutCandidate` to
contracts if scout is ever split out.

### M3. `benchmark.py` imports `marketdata.base.Bar` directly (OPEN, low-cost)

**Source:** architect agent, F3.
**Where:** `hawkeye/scout/benchmark.py:23`.

**Suggested fix:** promote `Bar` to `hawkeye/contracts/models.py` — it's
already the wire-format output of the (future) ingest service in
`ARCHITECTURE.md`'s service table.

### M4. `benchmark`/`review-passes` refetch 400 days of history per record with no caching, and skip order is biased toward newer records (OPEN)

**Source:** architect agent, F9. Partially overlaps the survivorship-bias
fix already shipped (that fix made *failures* visible; it did not fix the
*rate of* failure or the ordering bias below).
**Where:** `hawkeye/cli.py` (`cmd_benchmark`, `cmd_review_passes`) —
one HTTP fetch per record, no dedup by ticker, no cache; loop order is
`created_at` ascending (`store.py:111`), so once a rate limit kicks in
partway through, newer records are systematically more likely to be
skipped/censored than older ones.

**Failure scenario:** as records accumulate (100+), one `benchmark` run
means 100 external calls; a rate limit mid-run means the *missing* data
isn't random — it's time-correlated, which can bias a cohort comparison in
a direction that's hard to detect from the censored-count alone (the new
warning shows *how much* is missing, not that it's non-random).

**Suggested fix:** fetch once per unique ticker (dedupe across the record
list) and cache in-process; consider randomizing iteration order or at
minimum flagging when censored-count correlates with record recency.

### M5. Session-mode `finalize()` writes `case.recommendation_id` before the ledger insert is confirmed to have succeeded (FIXED — commit 472421e)

**Source:** architect agent, F10.
**Where:** `hawkeye/tribunal/casefile.py` (`finalize`, sets
`case.recommendation_id` and saves the case) vs `hawkeye/cli.py:188-193`
(the ledger `INSERT` that follows).

**Failure scenario:** a DB lock or disk-full error between those two steps
leaves a case file marked "complete, recommendation_id=X" with no matching
ledger row — `submit()` refuses to touch an already-complete case, so the
3 roles' work becomes unrecoverable from the CLI.

**Suggested fix:** write `recommendation_id` only after the ledger insert
succeeds (two-phase), or make `finalize()` idempotent so an incomplete
case can be re-finalized.

### M6. `Ledger` couples directly to `sqlite3`, no repository abstraction; calibration does N+1 queries (OPEN)

**Source:** architect agent, F11. Distinct from the concurrent-write race
that *was* fixed same day (python-reviewer's separate finding, now fixed
with `BEGIN IMMEDIATE` + busy_timeout — see `CLAUDE.md` 2026-07-28(b)).
**Where:** `hawkeye/ledger/store.py:71-75` (`Ledger.__init__` holds
`sqlite3.connect` directly, callers construct `Ledger(path)` directly —
`hawkeye/cli.py:66-67`); `store.py:246-257` (calibration: list-then-get-
per-id-then-get-events-per-id).

**Suggested fix:** not urgent at current scale (single local SQLite file,
single operator) — the "ledger service" migration path in
`ARCHITECTURE.md`'s table is aspirational, not close. Worth a `LedgerPort`
Protocol if/when that migration is actually planned; the N+1 in
calibration is cheap to flatten into one query whenever calibration
becomes slow enough to notice.

### M7. Session mode's information separation is enforced only for *file contents*, not for whether the orchestrator peeked (RULED: ACCEPTED LIMITATION)

**Source:** architect agent, F17; methodology-auditor (independently,
same root issue). Already taken to the user in the 2026-07-28(b) follow-up.
**Where:** `.claude/skills/hawkeye-run/SKILL.md` (prose instruction only);
`hawkeye/tribunal/casefile.py::write_package()` controls what's *written*,
not what the orchestrator *reads*.

**Status:** user's explicit ruling — "this can't be fixed within the
architecture (a subagent always inherits its parent session's access), so
disclose it honestly rather than pretend otherwise." Documented in
`CLAUDE.md` invariant 4, `docs/ARCHITECTURE.md`, `docs/MASTER_OVERVIEW.ja.md`
§4. **Do not attempt a code fix without new instruction** — the
methodology-auditor's suggested mitigation (log the exact subagent
invocation prompt into the case directory for post-hoc audit) was
considered and not required by the user, but remains available as a
cheap partial improvement if priorities change.

### M8. Two-driver equivalence test only checks the final decision, not the full record (OPEN)

**Source:** architect agent, F18.
**Where:** `tests/test_casefile.py::test_session_and_api_drivers_produce_identical_decisions`
asserts `verdict.decision` and a couple of other fields match; it doesn't
assert full payload equality.

**Failure scenario:** a future change to `finalize()` (session mode) that
isn't mirrored in `run_tribunal()` (API mode) — e.g. a new metadata field,
different NAV handling — would pass this test as long as the *decision*
still matches, silently letting the two engines drift on everything else.

**Suggested fix:** one-line strengthening — assert
`rec_api.model_dump(exclude={"id","created_at","model"})` equals the same
for `rec_session`. Catches all future drift in one assertion instead of
enumerating fields.

### M9. `hawkeye verify` doesn't cross-check `recommendations.hash` against the current payload (FIXED — commit 472421e)

**Source:** architect agent, F19.
**Where:** `hawkeye/ledger/store.py::verify_chain()` (`store.py:156-166`)
only walks the `journal` table. `recommendations.hash` (set once at
`store.py:88-93`) is never re-derived and compared against the current
`recommendations.payload` by any code path.

**Failure scenario:** rewriting a `recommendations.payload` row directly
(bypassing the Python API — e.g. manual SQL) and updating its `hash`
column to match would pass `hawkeye verify` cleanly. Invariant 1's claim
("technically impossible to rewrite history") is only fully true for the
journal, not for the recommendation body itself.

**Suggested fix:** ~10 lines — in `verify_chain()`, for each
`recommendation_recorded` journal event, recompute the payload hash from
the current `recommendations` row and compare against the hash recorded in
the journal event (not the mutable `recommendations.hash` column, which
could be rewritten in the same act of tampering).

### M10. Portfolio position cap is checked against a stale snapshot, not rechecked per candidate in a batch (RULED: ACCEPTED LIMITATION)

**Source:** architect agent, F20; independently corroborated by the
methodology-auditor. Already taken to the user in the 2026-07-28(b)
follow-up.
**Where:** `hawkeye/risk/sizing.py:62-63` — `open_position_count` is
computed once per `hawkeye scout --evaluate N`/`--open-cases N` run
(`cli.py:95`/`237`/`246`), not re-incremented as BUYs are produced within
that same run.

**Failure scenario:** holding 7 of 8 max positions, evaluating 3
candidates in one batch could produce 3 independent BUY proposals that
each individually see "1 slot free," collectively exceeding the cap if the
user approves all three.

**Status:** user's explicit ruling — invariant 5 (no autonomous trading,
user always executes) makes `hawkeye positions` a sufficient manual
backstop; not worth a fail-closed recheck at `record-entry` time. **Do not
implement a fix without new instruction.**

### M11. `scout_max_enrich` truncates on EPS-surprise order, but the final ranking formula weighs more than EPS surprise (OPEN)

**Source:** methodology-auditor.
**Where:** `hawkeye/scout/scout.py` — `screen_events()` sorts by EPS
surprise descending; `to_enrich = screened[:config.scout_max_enrich]`
truncates on that narrower criterion; `score_candidate()` (which also
weighs revenue surprise and event-day gap) only ever runs on the truncated
subset.

**Failure scenario:** a candidate with a modest EPS beat but an
exceptional revenue beat + ideal gap could score highest under the
system's own formula yet never be enriched at all, because the cheap
pre-filter used a narrower criterion than the real scoring function —
silently narrowing (and biasing) what "top N" means, and potentially
making the score formula look more/less predictive than it actually is
since it's only ever evaluated on an EPS-biased subsample.

**Suggested fix:** either enrich by a cheap proxy for the full score, or
at minimum log/measure how much this discards (the funnel already counts
`screened` vs `enriched` — cross-tabulating the discarded candidates' EPS
vs. revenue-surprise distribution would size the bias).

### M12. `Recommendation` is not structurally immutable (OPEN)

**Source:** python-reviewer.
**Where:** `hawkeye/contracts/models.py` — `Recommendation` has no
`model_config = ConfigDict(frozen=True)`.

**Failure scenario:** nothing currently mutates a `Recommendation` after
`assemble_recommendation()` returns it (verified — all callers respect
this), but invariant 1 ("payloads are immutable... never UPDATE") is
enforced by convention only, not by the type system. A future change
could silently violate it without any error.

**Suggested fix:** add `model_config = ConfigDict(frozen=True)` to
`Recommendation` (top-level only — `verdict`/`plan` are already finalized
by the time it's constructed).

### M13. `benchmark.forward_return` mixes calendar days with the doctrine's trading-day convention (OPEN)

**Source:** python-reviewer.
**Where:** `hawkeye/scout/benchmark.py::forward_return` — adds
`horizon_days` as *calendar* days (`timedelta(days=horizon_days)`) before
searching for the next bar, while `config.py`/`Thesis.expected_holding_days`
elsewhere treat holding windows as *trading* days.

**Failure scenario:** over a 4-8 week horizon this under-counts the
intended trading window by roughly 30% (5 trading days per 7 calendar
days), systematically shrinking the measured horizon vs. what the thesis
actually targeted — a horizon-definition mismatch in the Phase-0 primary
viability metric.

**Suggested fix:** convert `horizon_days` to a trading-day walk (index
into `bars` rather than a calendar delta), or explicitly document why
calendar days are intentional here and reconcile the wording elsewhere.

### M14. `case_id` is used to build a filesystem path with no format validation (OPEN, low real-world risk)

**Source:** python-reviewer.
**Where:** `hawkeye/tribunal/casefile.py::_case_path` —
`cases_dir() / f"{case_id}.json"`, and `case_id` comes straight from
`hawkeye case step/submit CASE_ID` CLI args.

**Suggested fix:** validate against the format `new_id("case")` actually
produces (`re.fullmatch(r"case_[0-9a-f]{12}", case_id)`) before building
the path. Cheap to close off even though the current threat model (single
trusted local operator) makes it low-priority.

---

## LOW severity

### L1. `tribunal` (session mode) can't be extracted into a standalone service — local filesystem is baked into the design (OPEN, honesty-in-docs item, not a bug)

**Source:** architect agent, F12.
**Where:** `hawkeye/tribunal/casefile.py` — case files live relative to
the process's CWD; role hand-off is "subagent writes to a local path,"
per `.claude/skills/hawkeye-run/SKILL.md`.

**Suggested fix:** no code change — `ARCHITECTURE.md`'s "any package could
become a standalone service" framing should note this is only true for
the API driver (`pipeline.py`), not the session driver.

### L2. `reports` → "notification service" migration is blocked transitively by M1/M2 (OPEN, consequence of M1/M2)

**Source:** architect agent, F13. Resolves automatically once M1/M2 are
fixed.

### L3. §5.1 (missed-candidate feature, not yet implemented) proposes storing the reject-pile record outside the hash chain — a structural self-dealing risk in the proposal itself (OPEN — design note for whenever §5.1 is implemented, not current code)

**Source:** architect agent, F14.
**Where:** `docs/MASTER_OVERVIEW.ja.md` §5.1, §6 — proposes treating the
new table like `scans` (append-only, outside the journal's hash chain).

**Concern:** this table would become the denominator for the Phase-0 kill
criterion — exactly the data with the strongest incentive to quietly edit
if results look unfavorable. Treating it as exempt from tamper-evidence
because "it's not something shown to the user" misses that it's evidence
the *project's own continuation* depends on.

**Suggested fix when §5.1 is actually implemented:** don't hash-chain
every row individually, but do anchor each scan's full reject-pile batch
as a single hash appended to the journal (one journal event per scan, not
per rejected candidate) — cheap, and closes the gap without the write
overhead the original proposal was trying to avoid.

### L4. §5.1's proposed write path (CLI-only) repeats the existing single-point-of-failure pattern in `record_scan` (OPEN — design note)

**Source:** architect agent, F15. Same category as M4/F8 — if a future
caller invokes `run_scout()` without going through the CLI (a scheduler, a
test harness, an alternate entry point), the recording silently doesn't
happen. Suggested fix when implemented: pass a recording sink into
`run_scout()` itself rather than making recording a CLI-layer
responsibility.

### L5. §5.1's proposed reject-pile schema doesn't reuse existing contract models, and the "lightweight enrichment-cap-only" tier can't be scored on the same formula as fully-enriched tiers (OPEN — design note)

**Source:** architect agent, F16. When implemented: define a
`ScreenedCandidate` contract reusing `GateResult`; tag records with a
`score_version` so cross-tier comparisons don't silently compare different
feature sets.

### L6. Case list sorts by filename, not by `created_at` (OPEN, cosmetic)

**Source:** python-reviewer. `hawkeye/tribunal/casefile.py::list_cases()`
sorts `case_*.json` glob results alphabetically; since `new_id()` uses
random hex, this isn't chronological despite `Case.created_at` existing.
Sort by parsed `created_at` instead.

### L7. `build_position_plan` is ~62 lines, slightly over the project's own 50-line guideline (OPEN, style only)

**Source:** python-reviewer. No behavior issue; veto checks and
share-sizing math are two separable concerns that could be split for
readability.

### L8. Training-data look-ahead protection is incidental, not explicit (OPEN, low priority)

**Source:** methodology-auditor. `max_event_age_days=10` (trading days)
happens to keep catalysts recent relative to any plausible model training
cutoff, but nothing ties this explicitly to model-swap safety — a future
`HAWKEYE_MODEL` change or freshness-threshold change could quietly weaken
this incidental protection. Suggested: one-line comment in `config.py`
making the dependency explicit.

### L9. No mechanical cross-check that Bull/Adversary claims match the structured dossier fields they reference (OPEN, low priority)

**Source:** methodology-auditor. Prompts correctly instruct trusting
structured `eps_surprise_pct`/etc. over prose (verified in the actual
prompt text), but nothing code-level catches a claim like "insider buying
is heavy" when `insider_activity` is null. A cheap regex/field-presence
cross-check would close this partially without a second LLM call.

---

## Documentation-only (not code bugs — doc-vs-code drift from the first review pass)

**Source:** doc-vs-code-consistency agent, first review pass. Not yet
corrected in the docs.

1. **`docs/MASTER_OVERVIEW.ja.md`'s "現時点で「記録に残っていないもの」"
   table** overstates the gap for the ranking-cutoff stage: it says
   "残らない" (nothing survives), but bare ticker symbols *do* survive in
   `scans.tickers` for that stage (confirmed against the live `hawkeye.db`)
   — only price/score/rank/gate-detail are actually missing. The
   higher-level conclusion (no *structured* missed-winner record exists)
   is still correct; the literal "zero trace" wording is not.
2. Same table cites "2026-07-22の実行" for the 28→15 enrichment example;
   the matching scan in `hawkeye.db` is actually dated 2026-07-21. Figures
   (28/15/13) are correct, only the date label is off by a day.
3. `docs/ARCHITECTURE.md` states "Judge sees the full written record and
   may not introduce new facts" in the same breath as claims that *are*
   code-enforced (visibility scoping). The "no new facts" rule itself is
   still prompt-only (`JUDGE_SYSTEM` text), not mechanically checked —
   distinct from the severity>=4-addressed rule, which *is* now
   code-enforced via `attack_id` matching (fixed same day as this review).
4. `docs/ARCHITECTURE.md`'s post-trade event chain
   (`user_decision → entry_trade → sentinel_signal → claim_resolution → exit_trade → outcome`)
   disagrees with `docs/MASTER_OVERVIEW.ja.md` §7.2's sequence diagram,
   which shows `hawkeye close` (exit_trade) *before* `hawkeye claims`/
   `resolve-claim` (claim_resolution) — the opposite order. No code
   enforces either order (`cmd_resolve_claim`/`cmd_close` are independent,
   callable in any sequence); the two docs should agree with each other
   and with how `SKILL.md` actually walks a user (which matches
   MASTER_OVERVIEW's order, not ARCHITECTURE's).

---

## Explicitly out of scope / not a new backlog item

- Same-model correlation across Bull/Adversary/Judge (all Claude) — already
  self-flagged in `docs/VERIFICATION_PROTOCOL.md`/`docs/ROADMAP.md` as a
  Phase-2 item ("role independence... when verdict patterns suggest
  same-model correlation"). Structurally irreducible at single-vendor MVP
  scale; not something to fix now.
- "Calibrated probability"/"Brier-scored" framing being an unvalidated
  claim — not actionable until claims/outcomes accrue (0 closed positions
  currently); revisit once `hawkeye calibration` has real data.
- Doctrine-change discipline ("decide numeric thresholds before looking at
  results") having no *technical* enforcement, only a commit-message
  convention (`CLAUDE.md` §7) — an organizational/process concern, not a
  code fix.
