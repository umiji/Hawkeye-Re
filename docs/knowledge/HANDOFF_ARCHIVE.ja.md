# 引き継ぎログ(アーカイブ)

2026-07-12 〜 2026-08-03 に `CLAUDE.md` の末尾へ書き足されていた記録を、そのまま
移したものです。**追記はもうしません** — 再開に必要な状態は
`/ecc:save-session` が書くセッションファイルに、後々まで効く教訓は
`docs/knowledge/` の各ファイルにあります(索引は `README.md`)。

歴史的な文脈を辿るときだけ読んでください。ここに書かれた「未実装」「次にやること」の
類は、**書かれた時点の状態**であり、現在の状態ではありません。

---

## Session hand-off log

Record decisions and insights at the end of each working session
(newest first).

- **2026-08-03** The Goal the previous session missed is **reached**. All four
  gaps it listed are closed, and the two ordered pieces of work landed under
  TDD. **375 offline tests green** (348 before); ledger chain ✅.

  **What now runs in production, not only in tests.** (1) `hawkeye consensus
  capture` write path executed for real: **841 consensus rows pre-registered**
  for prints due 2026-08-04..05, from 855 scheduled names (52 Yahoo misses, 14
  with no estimate at all from either source → no row, by design). Worth
  knowing: the window is `business_days_ahead(today, 2)` = **tomorrow and the
  day after — NOT today**, so a name reporting after today's close has no
  pre-registration unless yesterday's run caught it. Not changed unilaterally;
  flag it to the user. (2) `hawkeye scout` ran end to end against live
  Finnhub/Yahoo for the first time: window 2026-07-31 only (derived from the
  2026-08-01 run), 69 events → 14 screened → **all 14 already seen → 0
  enriched, 0 candidates**. A legitimate run with nothing fresh to chew on,
  not a failure. Its verification pass is the number to remember: **16 names
  re-read from Yahoo, 13 of them materially disagreed with the calendar**.
  (3) The **AMZN 疑似審査 actually ran** — `hawkeye case open AMZN
  --from-earnings --event-date 2026-07-31`, then Bull / Adversary / Judge as
  three isolated subagents, verdict 見送り (conviction 0.30), recorded as
  `rec_83296621f94b`. The Judge's decisive finding was arithmetic inside the
  Bull's own numbers: entry 271.58 against the Bull's own stop 243.50 is
  −10.3% of risk for a +3.8% base case, 0.37:1. Two of the three severe
  attacks were about **our own data**: zero verified legs, and an
  "underreaction" edge contradicted by a headline in the Bull's own dossier.

  **The one Goal item that could not be met, and why it never could.** AMZN's
  revenue leg is still **未検証** — one consensus source. Nothing recovers a
  snapshot after the release, so this was only ever demonstrable on a FUTURE
  print; the 841 rows above are what makes the next one two-sourced. Do not
  re-attempt it on AMZN Q2.

  **Two features, both pre-approved designs.** (a) **The release read at the
  tail of the funnel** (§5.3 実装順序(c), the user's explicit order):
  `hawkeye/scout/release.py`. The read happens OUTSIDE the process — no free
  structured source for guidance or non-GAAP exists — so the funnel **names
  the prints it wants** (`release_wanted`, printed with the exact path) and
  picks documents up from `var/releases/<TICKER>_<date>.json` next run. An
  extraction is discarded whole unless its GAAP EPS equals EDGAR's filed
  value. **Where the company published no adjustment the dispute stays open**
  and the row still deepens: settling it with XBRL alone is the rejected
  shortcut (XBRL says which value was FILED, never which basis the CONSENSUS
  is on — AAPL 2.02 vs 1.91). (b) **The quarterly history gap** (§6.1(C)):
  every screened name and every name already in the master keeps its quarter,
  written after the enrichment walk so a deeper reading is never shallowed.
  Effect visible immediately — `earnings_prints` went 0 → 14 rows on the first
  production scan.

  **One thing built beyond the two orders, because the Goal could not be met
  without it**: `hawkeye case open TICKER --from-earnings`
  (`hawkeye/scout/single.py`). AMZN's calendar rows collapse to +2.7%, so the
  discovery screen drops the print before verification — the print this whole
  design came from was unjudgeable in the product. The screen decides who is
  worth looking at when nobody asked; once a person names a stock, that
  question is answered. Same verification, same both-sources rule, same pinned
  consensus, same recorded quarter as the funnel.

  **Not done, deliberately**: `stock_id` still not back-filled (user's
  instruction); the ledger check "do any of the 15 recorded recommendations
  rest on numbers where the sources disagree?" is **still unrun** (now 16
  recommendations); `test_verification_recovers_a_name_the_calendar_wrongly_
  screened_out` still unrevisited.

- **2026-08-02(c)** Implementation session: the whole of §5.3/§6.1 was built
  under TDD (`47f172c` docs, `64f41e6` core, plus this entry's commit).
  **347 offline tests green** (271 before). New surfaces: `hawkeye consensus
  capture`, `hawkeye stocks show|list|rebuild`, and a live rehearsal script
  `scripts/dry_run_earnings_quality.py` (outside `tests/` — it does real
  network I/O; the suite stays fully offline).

  **What is now enforced in code.** A beat requires BOTH sources to say so;
  a disputed consensus (>5%) ranks on the conservative reading; disputed
  actuals (>2% AND >$0.01) are unverified until the release settles them;
  Finnhub's actual is unusable when its own rows contradict each other;
  non-GAAP is read, never computed. `consensus_snapshots` and
  `earnings_prints` refuse UPDATE/DELETE **at the SQLite level** — an API
  without an update method is only a convention, and a decision references
  those rows by id (invariant 1 survives by reference, not by copy). A
  quarter deepens by APPENDING a row per `depth`, never by rewriting one.

  **Four things only implementation revealed** (all fixed; detail in
  `docs/design/MASTER_OVERVIEW.ja.md` §5.3(8)). (1) **Yahoo's estimate periods are
  relative to today, not to the last print** — measured on AMZN three days
  after its Q2 release, `0q` read 1.956 while the consensus that print was
  judged against was 1.83, and the row's own growth field said +0.3% where Q2
  grew 242%. So a post-print reading is one quarter out; `shift_after_print()`
  now uses it ONLY as the guidance yardstick, which is what the quarter now in
  progress actually is. This is a stronger argument for pre-registration than
  the design had. (2) **The unquantified-one-off case (§5.3 決定1③) is real**:
  AMZN disclosed $53.4B pre-tax at the consolidated level and no per-share
  figure anywhere, so the print travels as `unadjusted` — first measurement of
  a share the design listed as unknown. (3) **A missed leg used to score like
  missing data**, so AMZN (EPS +194%, revenue −3.5%) took the capped maximum
  and outranked a name that beat on both; a miss now subtracts what the same
  magnitude of beat would add (no new constants — the existing caps, mirrored).
  (4) A stored warning that never printed: the `unadjusted` flag was recorded
  but absent from the Japanese verdict. Also fixed a latent bug — nothing ever
  created `var/`, so any fresh `HAWKEYE_VAR` killed every command.

  **Operational requirement, not optional**: run `hawkeye consensus capture`
  every business day during earnings season. Without a pre-registered row the
  revenue leg has one consensus source and is therefore *unverified* — one of
  the three legs is effectively unavailable (that is exactly what the AMZN
  rehearsal showed). Nothing recovers a snapshot after the release.

  Not done, deliberately: `stock_id` is NOT back-filled into the existing
  tables (the user's instruction is that recorded rows are never rewritten),
  so history still joins by ticker; guidance extraction is not automated
  inside `hawkeye scout` (the rehearsal script takes it via `--guidance`, and
  an extraction is rejected outright unless its GAAP EPS equals EDGAR's XBRL
  value). Still unrun: the ledger check "do any of the 15 recorded
  recommendations rest on numbers where the sources disagree?".

- **2026-08-02(b)** Measurement + design session, **no code written**, working
  tree clean at `a0f8419`. Started from one question — AMZN printed EPS 5.75
  against a 1.83 consensus (+215%); how does the system treat a beat that is
  really a one-off? — and ended with an approved data-source design. Full
  detail, including every measured number, is in
  `~/.claude/session-data/2026-08-02-aade8f79-session.tmp`; read that before
  implementing.

  **What the measurements showed.** AMZN's +215% is an artifact: EDGAR's 10-Q
  (filed 22:11 UTC, ~2h after the 8-K) shows non-operating income of $53.4B
  against $27.5B of operating income; normalized, the beat is ≈+6.7%. AAPL is
  the same failure in the opposite direction and it broke my reasoning: Apple's
  release says diluted EPS 2.02 "included a favorable impact of $0.11 from
  tariff refunds" — 2.02−0.11 = **1.91 exactly**, which is what Finnhub (and
  Earnings Whispers) report. The one-off sat **inside gross margin**, so
  `NonoperatingIncomeExpense` ($0.572B) shows nothing: **checking only the
  non-operating box to conclude "no one-off" is a method error.** An unbiased
  50-name survey off the 2026-07-27..31 calendar (stride-sampled, deliberately
  NOT filtered by the screen under test) then gave the real base rates:
  **reported EPS disagrees between Yahoo and Finnhub in 25% of names, and the
  source choice flips the 5% screen verdict in 19% — but 6 of those 9 flips
  have IDENTICAL actual EPS.** The damage is mostly in the CONSENSUS, not the
  actual. Four of them (TBBK/PCAR/CARR/TRS) sit on the 5% boundary with
  consensus differing by only 1–2%, which no fix to the actual can help.

  **Three prior beliefs overturned — do not re-inherit them.** (1) "Finnhub
  stamps the prior quarter onto AAPL" (2026-08-01 log) is wrong about the
  value; 1.91 appears nowhere in Apple's history. What is true is that
  `stock/earnings` attaches the SAME actual to two different period rows — a
  period-alignment defect. (2) "Yahoo right, Finnhub wrong" is too simple:
  on BJRI (adj 0.94/GAAP 0.86) and CDNA (0.37/2.07) **both vendors returned the
  adjusted figure and agreed**; they diverge only where no adjusted EPS is
  published. And INVH is the counter-example that kills any "Finnhub is street
  basis" rule — it compared a 0.37 FFO-style actual against a 0.1771 EPS
  estimate for +108.9%, mixing bases inside one row. (3) My own mid-session
  claim that structured data suffices and prose extraction is unnecessary is
  refuted by AAPL. Also flagged for revisit: last session's celebrated
  `test_verification_recovers_a_name_the_calendar_wrongly_screened_out` —
  AAPL's like-for-like beat is ~+1%, so that "recovery" may have admitted a
  one-off-inflated false positive.

  **Decisions.** A beat requires **both** sources to agree it is a beat.
  Neither vendor is "the source of the actual" — agreement (75%) is the answer;
  divergence escalates to the company's own filing. **Non-GAAP is never
  computed**, only read: company-published value → else GAAP minus a disclosed
  per-share one-off → else `unadjusted` and passed to the tribunal as a known
  unknown. If Finnhub's duplicate rows disagree on the actual (AMZN 1.88 vs
  1.97) its actual is unusable for that print — **never pick whichever row
  matches Yahoo**, since matching is not evidence of correctness. Thresholds
  come from measured gaps, not invention: escalate to the 8-K when actuals
  differ by >2% and >$0.01 (the distribution has a clean gap between 1.1% and
  2.9%); flag `consensus_disputed` when estimates differ by >5% (gap between
  3.50% and 5.18%) and **rank on the conservative reading** — extending the
  existing collapse-to-conservative rule across vendors, so BIIB ranks +20%
  not +77%. An actual disagreement is resolvable by a document; **a consensus
  disagreement is resolvable by nothing, because no primary source for
  consensus exists anywhere.** Guidance: case-split, never a gate, small
  positive weight, **absence is neutral not a penalty**; it is tribunal
  material rather than a discovery signal (accepted consequence: a name with
  mediocre EPS and excellent guidance is never surfaced).

  **The enabler.** `yfinance`'s `earnings_estimate` / `revenue_estimate` /
  `eps_trend` return 0q and +1q consensus with `numberOfAnalysts` and
  low/high — so consensus can be **pre-registered before the print**. That is
  what gives revenue a second opinion (after the print, Finnhub is the only
  source and Yahoo's calendar carries no revenue at all), what makes the
  agreed n≥3 floor implementable (INVH's EPS consensus has **n=1**), and what
  fixes vintage — BIIB's estimate moved 3.98→2.15 over 90 days, so vendor
  disagreement is plausibly *when* they sampled, not *how*. Guidance has **no
  structured source at all** (`company-guidance` returns HTML, `eps-estimate`
  / `revenue-estimate` / `price-target` 403), and Finnhub cannot supply an
  analyst count or range on this tier, so the double-confirmation is
  **asymmetric**: Yahoo gives a distribution, Finnhub a single point.

  **Cost, since it shaped the design.** Reading every 8-K is ~8,900 tokens/name
  and naive slicing saves only 31%; the guidance section alone is ~1,800. So
  the read is placed at the tail of the funnel (10–15 names) and fires on
  divergence only — ~1 reconciliation read per run, ~20k extra tokens, against
  a tribunal that already dominates. Extraction is validated mechanically:
  extracted GAAP EPS must equal EDGAR's `EarningsPerShareDiluted` or the
  extraction is rejected, which catches the "read the year-ago column" failure
  for free; no XBRL (26% of names) means `unverified`, never trusted.

  **Schema decision.** The user observed correctly that everything is keyed on
  the scan/decision axis — `ticker` is a bare TEXT column on `recommendations`
  / `scans` / `screened_candidates` / `drop_reviews` and there is no entity
  table, so a per-ticker history means scanning every decision record. Agreed
  to add `stocks` / `earnings_prints` / `consensus_snapshots` (additive
  only; `verify_chain()` is unaffected by new tables). **The key must be CIK,
  not ticker** — tickers are reused after delisting and change on renames, and
  keying on them would silently merge two companies and re-break the
  survivorship-bias analysis fixed on 2026-07-28. Per the user, new records
  must **not** duplicate into their payload anything held in the new tables
  (existing rows untouched). That is safe for invariant 1 **only if** consumed
  snapshot rows are append-only and the decision stores the row id it used —
  pre-registration then survives by reference instead of by copy. **Enforce
  insert-only in the store layer, not by convention**; an UPDATE path added
  later would break invariant 1 silently.

  **Settled on review the same day** (design written up in
  `docs/design/MASTER_OVERVIEW.ja.md` §5.3 + §6.1): the master is named `stocks`, not
  `securities` — "security" collides with the information-security sense
  throughout the code. It carries a *projection* of the LAST review only
  (`last_reviewed_fiscal_quarter` as `2026-Q2`, `last_reviewed_at`, stage reached),
  rebuildable from the ledger. **Holding status is deliberately NOT on the master**
  — a stock has n positions over time, so it belongs in the `positions` projection
  (§5.2(2), still unimplemented); the master must not depend on a table that does
  not exist yet. Nor does it carry **point-in-time observations** (price, market
  cap, analyst ratings) — those are frozen inside the immutable decision payload,
  and a mutable "current price" on the master is a look-ahead path for later
  analysis.
  `earnings_prints` carries a `depth` (`calendar_only` → `verified` →
  `xbrl_validated` → `release_read`) so a quarter we never looked at stays
  distinguishable from one we looked at and found nothing in; `calendar_only` rows
  cost nothing (the values are already in the calendar response), so any name that
  entered the funnel keeps an unbroken quarterly history. The user proposed
  updating one consensus row per (stock, quarter) in place and freezing it after
  the print — **declined, with reasons**: a decision references the row by id, so
  an update silently repoints a pre-registered recommendation at different numbers;
  the revision path is itself the evidence behind the vintage-not-methodology
  conclusion (BIIB 3.98 → 2.15 over 90 days); and it destroys the
  pre-registered/reconstructed distinction. The "fix" the user wanted is a pointer
  instead — `earnings_prints.consensus_snapshot_id` = the row in force just before
  the print. Snapshotting for quarter Q stops when Q's print is recorded (later
  captures belong to Q+1), and a capture identical to the newest row writes no
  row — so in practice a term holds ONE row, and a second row exists only because
  the estimate actually moved. Capture covers names reporting within the next
  **2 business days**, not strictly T−1: runs are manual, and one missed day would
  lose that print's snapshot permanently (same reasoning as the scan window in
  §5.2(1)). Cost scales with API calls, not rows — 62 calls/min measured,
  ~150–200 names/day at peak, so ~5–7 min per run.

  Session cost ~$65 (one CRITICAL warning) — all of it live measurement.
  Nothing needs re-measuring; `basis_survey.json` in the scratchpad holds the
  raw sample. `docs/design/MASTER_OVERVIEW.ja.md` §4/§5 is **not yet updated** — the
  governance rule requires that, with approval, before implementation begins.

- **2026-08-01(b)** First real session-mode run since the tree split: scout
  opened 3 cases (BJRI/ABR/CRI), all three PASSed. The run itself was fine;
  what it surfaced was not. Two convergent observations from the role
  subagents — which argue from separate contexts and cannot compare notes —
  turned out to be real defects, investigated with systematic-debugging
  before any fix.

  **(1) The ranking metric was broken, and it was the metric deciding which
  candidates were ever examined.** `screen_events` sorted by raw EPS surprise
  % and `to_enrich = screened[:scout_max_enrich]` cut the top 15 by that same
  order. Three independent ways the percentage lies, all verified against
  live Finnhub responses: (a) the calendar returns *several rows for one
  print* with different quarter labels and different consensus — BJRI's
  2026-07-30 came back as both +3.5% (est $0.9085) and +633.2% (est $0.1282);
  sorting by surprise meant the broken row always won, and the correct row
  then fell below the 5% screen, so the only surviving reading was the wrong
  one. (b) `revenueActual` and `revenueEstimate` can be on different
  accounting bases — ABR's actual is gross interest income (230.9M) against a
  net-basis estimate (50.7M), i.e. "+355.3%". (c) a near-zero consensus makes
  the ratio explode without adding information; the recorded drop pile was
  topped by CORT +6958%, SONO +5194%, LXP +3459%, overwhelmingly REITs (GAAP
  EPS ≈ 0, real earning power is FFO). Both of that day's top-scored
  candidates (BJRI 85.0, ABR 85.0 — the cap-saturated maximum) were artifacts.
  Fixed, user picked the full three-part remedy: `parse_calendar` collapses
  rows sharing (ticker, day) to the *conservative* reading and flags
  `conflicting_estimates`; percentages below `scout_min_abs_eps_estimate`
  (0.10) or beyond `scout_max_trusted_revenue_surprise_pct` (50.0) are marked
  untrusted; ranking is by the *capped* score with untrusted values scoring 0.
  Untrusted means "cannot buy a ranking slot" — NOT dropped (still recorded,
  with the trust flags, so a later drop review doesn't read "+6958%, dropped"
  as a missed monster) and NOT passed through as fact: a distrusted number is
  kept out of the structured snapshot fields entirely, because the prompts
  tell Bull and Adversary to prefer those over prose, so leaving it there
  would launder it into a fact. The catalyst text says what was measured and
  why it isn't stood behind. Verified against the real rows: BJRI now reads
  +3.5% and drops below the screen, CRI 66.23 → 1.23, ABR 85.0 → 0.0, and a
  genuine +40%/+5% name ranks first.

  **(2) `Claim.id` was a random uuid, so `parse_thesis` was not
  deterministic.** Session mode parses the thesis three times — Adversary
  package, Judge package, finalize — so each role saw a different set of
  `clm_` ids; the Adversary cited ids the Judge could not find, and neither
  matched the ledger. The comment in `casefile.py` asserting determinism was
  simply false. Same bug class as the 2026-07-28 attack-id fix, and the same
  remedy: `claim_content_id()`, a before-validator that fills only an absent
  id (stored payloads keep theirs — invariant 1). Damage was bounded today
  because claim resolution reads ids off the stored record, but any future
  rule matching claim ids across parses would have failed silently, exactly
  as `_judge_rule_check` once did. **Not fixed, worth knowing:**
  `parse_attack_report` still honours an LLM-supplied `id` (`a.get("id") or
  ...`) even though its docstring says ids are never the LLM's choice — the
  schema doesn't expose the field, so it can't happen today, but the guard
  is prose rather than code.

  226/226 offline tests green (216 + 10 new). Docs: MASTER_OVERVIEW §4 gained
  both write-ups and §5 a new "サプライズ率の信頼性" row recording the
  deferred option — replacing the percentage with a price-normalized surprise
  ((actual − estimate) / price) is the principled fix but needs a price for
  every screened name *before* ranking, so it waits until the trust-flagged
  data shows how much distortion the current remedy leaves.

- **2026-08-01** Three planned steps, each committed after a green run.
  (1) `26b7ad8` — split the tree by *who writes the file*: `strategy/`
  (investment knowledge a human writes or approves) vs `docs/` (system
  design) vs `var/` (everything the system emits at run time, git-ignored).
  `hawkeye/paths.py` is now the single resolver for runtime locations;
  `HAWKEYE_VAR` moves the whole tree. Investment standards deliberately stay
  out of `.claude/` — API mode never reads it, so criteria kept there would
  silently depend on which engine ran the tribunal.
  (2) `18835ba` — the drop-candidate review round (`drops measure/queue/
  submit/revise`, driven by the new `/hawkeye-review` skill in its own
  session). The measurement engine and the table both existed but nothing
  joined them: no CLI path ever called `record_drop_reviews()`. Design
  points now enforced in code: only T+10 is investigated (a name looked at
  twice would double-count in the 20-per-category tally); every measured
  candidate is recorded, not only outliers (no denominator = "3 got away"
  reads as neither good nor bad); `recorded_drop_review_keys()` stops a
  round re-measuring what it cannot store. **Split `unforeseeable` into
  `collection_gap` + `unforeseeable`** — "nobody could have known" is the
  one category that ends an inquiry, so our own collection defects (narrow
  news window, single source) were accumulating inside the one verdict that
  requires no follow-up. The investigator now receives what we held at
  decision time alongside a fresh fetch **cut at the decision date in code**
  (articles published later are never handed over — the same reasoning as
  invariant 4), and `submit()` overturns `unforeseeable` to `collection_gap`
  when the record shows the news was public in time (invariant 3). Renamed
  `drop_review_min_samples_per_stage` → `_per_category` (value 20 unchanged).
  (3) `98b6e49` — `strategy/TRIBUNAL_ROLES.ja.md` generated from
  `prompts.py` (`hawkeye docs tribunal-roles --write|--check`). Prompts stay
  in `prompts.py`; a new numbered Judge rule fails generation until it gets
  a Japanese gloss. 216/216 offline tests green.

  **Two things worth knowing.** (a) `cases/` (12 case JSONs) vanished during
  this session — cause never identified; not reproducible from the test
  suite, not in the recycle bin, and the code that deletes directories
  (`_remove_role_workspace`) can only touch `cases/<case_id>/`, never the
  parent. The ledger was unaffected (chain verified, 12 recommendations
  readable). (b) Prompted by that, the user **downgraded the case JSON from
  "audit trail" to "debugging convenience"**: it does hold the LLM's raw
  pre-normalization reply which the ledger lacks, but no code compares the
  two and the file sits in git-ignored `var/` outside the hash chain, so its
  loss is undetectable. Claiming audit value for a file with neither a
  reader nor tamper-evidence is the worst combination — nobody dares delete
  it, nobody notices when it goes. If that comparison ever genuinely needs
  to happen, the raw values belong in the ledger, hash-chained. Docs and
  test docstrings updated to say this plainly.

- **2026-07-29** User recovered the full architecture review finding list
  at `docs/design/ARCHITECTURE_REVIEW_BACKLOG.md` (the 2026-07-28(b) entry below
  had marked it unrecoverable — that note is now stale, read the backlog
  file instead). Worked through it in two rounds, prioritizing
  integrity/consistency findings first per user request: (1) H1 — Adversary
  and Judge argued over the Bull's raw, un-normalized thesis dict in both
  API mode (`pipeline.run_tribunal`) and session mode
  (`casefile.write_package`) instead of the parsed/clamped `Thesis` that
  actually gets stored, so the debated record and the stored record could
  disagree. Both call sites now parse once and render the normalized model.
  (2) M5 — session-mode `finalize()` persisted `case.recommendation_id`
  before the caller confirmed the ledger insert succeeded; a crash in
  between left a case looking "complete" with no matching ledger row, and
  `submit()` refuses to touch an already-answered role, making the work
  unrecoverable. `finalize()` no longer sets `recommendation_id`; new
  `mark_complete()` does, called only after the ledger write succeeds;
  `cmd_case_submit` detects and retries an unconfirmed-but-role-complete
  case instead of erroring. (3) M9 — `verify_chain()` never cross-checked
  `recommendations.payload` against anything, so rewriting it directly via
  SQL (and updating its own `hash` column to match) passed silently; it now
  recomputes the payload hash and compares it against the tamper-evident
  `payload_hash` captured in the `recommendation_recorded` journal event.
  (4) H5 + M13 — `hawkeye benchmark --horizon` was unpinned (could be
  re-run at whatever value makes the spread look favorable) and
  `forward_return` added the horizon as *calendar* days while every other
  holding-period convention in the doctrine is *trading* days (~30%
  under-count over a multi-week span). User approved pinning the official
  Phase-0 horizon at 30 trading days
  (`config.phase0_benchmark_horizon_days`); `forward_return` now walks
  `bars` by index (trading-day-native) instead of adding a calendar delta;
  added `min_calendar_days_for_trading_days()` so the pending-vs-censored
  pre-filter converts correctly instead of mislabeling genuinely-still-
  pending records as "fetch failed". 87/87 green after this round
  (commits 472421e, 99678a4).

  User then asked to actually start running the candidate-selection cycle
  (nothing held currently) and specifically wanted dropped candidates
  recorded — this is exactly §5.1's proposal (missed-candidate tracking),
  written 2026-07-14(b) and never implemented. Implemented the recording
  MVP (deferred the analysis-loop / beta-alpha benchmark refinement in
  §5.1, since those need accumulated data to mean anything): new
  `ScreenedCandidate` contract (stages: enrichment_cap, gate_reject,
  ranking_cutoff); `Ledger.record_screened_candidates()` persists the batch
  and anchors its integrity as ONE `screened_candidates_recorded` journal
  event (per-row hash-chaining wasn't worth the write overhead, but
  `verify_chain()` now cross-checks the batch hash the same way it does for
  `recommendations` — same M9 pattern, applied proactively here); `hawkeye
  scout` now records automatically every run, `hawkeye screened list`
  reviews what's recorded. Also fixed a bug §5.1 had specifically flagged
  while reading the design doc: rejected/capped candidates never had
  `score` computed (stayed at the dataclass default 0.0), which would have
  made any later score-vs-return correlation check meaningless — score is
  now computed for every candidate that gets far enough to have the data
  for it (gap-aware "full" once a brief is built, "partial_no_gap"
  otherwise, tagged via `score_version`). Separately found and fixed a
  live blocker while smoke-testing: `hawkeye --help` (and any command
  printing an em dash or emoji) crashed with `UnicodeEncodeError` on
  Windows — `sys.stdout`/`stderr` default to the system codepage (cp932),
  same bug class as commit 01152f2's file-I/O version, just for console
  streams. Fixed by reconfiguring both to UTF-8 at the top of `main()`.
  91/91 offline tests green.

- **2026-07-28(b)** Follow-up to the same-day review below: worked through
  the leftover findings that were only summarized (not saved verbatim —
  the transcript that produced the full 21-item architecture list and
  doc-vs-code drift catalog had already ended, so only this log's summary
  survived). Fixed two concrete, scoped bugs directly (no design decision
  needed — both restore an already-documented invariant rather than change
  behavior): (1) `casefile.list_cases()` silently skipped unreadable case
  files (`except Exception: continue`); now prints a warning to stderr with
  the file path before skipping, so a corrupted case doesn't just quietly
  vanish from `hawkeye case list`. (2) `Ledger.append_event()`'s
  read-prev-hash-then-insert was two separate statements with no shared
  transaction; two processes appending concurrently could both read the
  same `prev_hash` and each insert a row claiming it, corrupting the hash
  chain in a way `verify_chain()` can only detect after the fact, never
  undo. Fixed with an explicit `BEGIN IMMEDIATE` transaction around the
  read+insert (plus `PRAGMA busy_timeout=5000` so a second writer waits
  instead of erroring). New regression test spawns 6 threads × 15 events
  each against one SQLite file and asserts the chain still verifies.
  Two further findings were architecture-level and taken to the user per
  the governance rule above instead of being fixed unilaterally: (a)
  portfolio-cap (`max_positions`) is checked in `build_position_plan()`
  against an `open_position_count` snapshotted once when a case opens, not
  re-checked when the position is actually entered — concurrent evaluations
  could each see "one slot free" and jointly exceed the cap once both are
  manually entered. User's call: leave as-is — invariant 5 (no autonomous
  trading, user always executes) makes `hawkeye positions` a sufficient
  manual backstop; not worth a fail-closed recheck at `record-entry` time.
  (b) Session-mode role separation (`write_package()`) is code-enforced only
  for *what's written into each role's input file* — nothing stops the
  orchestrating Claude Code session itself (which has full filesystem
  access to the case directory) from reading another role's raw output
  before spawning the next subagent; the real boundary is the SKILL.md
  instruction not to. User's call: this can't be fixed within the
  architecture (a subagent always inherits its parent session's access), so
  disclose it honestly rather than pretend otherwise — documented in
  `docs/design/MASTER_OVERVIEW.ja.md` §4, `docs/design/ARCHITECTURE.md`, and invariant 4
  above. 81/81 offline tests green (79 prior + 2 new; `test_llm_auth.py`
  still excluded, pre-existing unrelated collection failure, still
  out of scope). The remaining ~19 architecture findings and the
  doc-vs-code drift catalog from the original 2026-07-28 review are still
  unrecovered (never saved outside that ended conversation) — if a future
  session needs them, they must be re-derived by re-running a review, not
  looked up.

- **2026-07-28** User asked for an architecture/design/code review to
  confirm the system can actually deliver on its stated goal. Ran 4
  independent parallel reviews (doc-vs-code consistency, architecture
  soundness, python code quality, adversarial investment-methodology
  audit). Two CRITICAL findings were reached independently by multiple
  reviewers with no shared context — treated as high-confidence, not
  coincidence:
  (1) `_judge_rule_check` enforced "severe attack addressed" via a
  60-character substring match against `attack_statement`, not semantic
  content. A judge that paraphrases (any real LLM) fails this check even
  after genuinely refuting the attack, silently overturning a correct BUY
  to PASS with an authoritative-looking "[RULE ENFORCEMENT]" note. This is
  the likely real cause of the 2026-07-14 "3/3 candidates PASSed via rule
  enforcement" result — previously read as a healthy outcome, now suspect.
  **Fixed**: `Attack` gets a stable, content-hashed `id`
  (`_content_id()`, sha256 of category+statement+evidence — deterministic,
  not random, so two independent parses of the same raw dict agree); the
  Judge must now cite `attack_id` in `addressed[]`; `_judge_rule_check`
  matches by id-set membership. Both API and session drivers render an
  ID-tagged, already-parsed attack view to the Judge instead of the
  Adversary's raw JSON. Regression tests added proving a paraphrased
  statement with the right id still counts, and a wrong/missing id still
  overturns BUY. Old ledger rows keep loading (`attack_id` defaults to
  `""`), unaffected since Recommendation payloads are immutable.
  (2) An unverified hard entry gate (e.g. missing market cap from a
  Finnhub free-tier gap) returned `passed=True, unverified=True` and was
  never counted as a hard failure — contradicting invariant 6 in spirit
  (labeled unverified, but not blocking). Investment risk: an illiquid or
  micro-cap candidate could reach the LLM tribunal and get a real BUY with
  no way to exit cleanly at the pre-registered stop. **Fixed (fail-closed,
  user's explicit choice over a soft unverified-count threshold)**:
  `GateReport.hard_failures` now includes unverified hard gates. Also
  exposed a latent bug in the shared test fixture `make_bars()` (stopped
  ~30 days short of `end` for n=300, masked until now by the old
  silent-pass gate behavior) — fixed to anchor exactly on `end`.
  Separately, the methodology audit found the *already-shipped*
  `hawkeye benchmark` cohort comparison — the actual Phase 0 kill-criterion
  measurement — had the same two failure modes `docs/design/MASTER_OVERVIEW.ja.md`
  §5.1 warns about for the *proposed* future feature: manual `evaluate`
  picks were never filtered out of viability stats despite
  `strategy/ROADMAP.md` requiring it, and any ticker whose price history fetch
  failed (delisted/acquired/API outage) was silently dropped rather than
  flagged — survivorship bias, since failed-fetch tickers are
  disproportionately the worst performers. **Fixed**: new
  `hawkeye/scout/benchmark.py::collect_samples()`; `hawkeye benchmark`
  defaults to `--source scout` and reports per-cohort censored counts with
  an explicit survivorship-bias warning instead of a silent skip count.
  Full test suite: 79/79 green (one pre-existing, unrelated collection
  failure in `tests/test_llm_auth.py` — imports a function that doesn't
  exist in `hawkeye/tribunal/llm.py` — left unfixed, out of scope). The
  remaining review findings (doc-vs-code drift: a few factual inaccuracies
  in ARCHITECTURE.md/MASTER_OVERVIEW.ja.md; ~19 further architecture
  findings from portfolio-cap-not-enforced-across-concurrent-evaluations to
  ledger hash-chain race conditions to session-mode role separation being
  prose-enforced rather than code-enforced; code-quality HIGH findings
  around ledger concurrent-write races and silent case-file-read failures)
  were reported but not acted on this session — not yet triaged into this
  log or the backlog.
- **2026-07-14(c)** User asked for a "50%必達" strategy/tactics audit against
  `MASTER_OVERVIEW.ja.md`. Verdict: current design is a strong bias-removal
  experiment but the return math doesn't close — modeled ceiling ~+47%/yr
  under aggressive assumptions (p=0.55, 8 slots fully turning), realistic
  case ~+13.5%/yr once occupancy (~70%) and profit-taking-at-target (no
  trailing/runner logic, avg realized ~1.3R not 2R+) are modeled. Missing
  levers identified: trade-count (single detector = zero candidates in
  earnings off-season), right-tail capture (target hit = review, not
  partial-exit-and-trail), and a pre-registered path to raise risk_pct
  (currently ~1/43 of full Kelly at p=0.55/2:1 payout — literal numbers
  belong to the reader's own priors, not a forecast). Also flagged missing
  defenses: no portfolio-level drawdown circuit breaker, no regime filter,
  no sector-concentration cap. Wrote `strategy/STRATEGY_BACKLOG.ja.md`: full
  review + 12-item backlog (BL-01..12) tiered by cost/measurement-impact
  and sequenced against current dev state (Phase 0 has 0 open positions,
  3 evaluated candidates — cheapest possible time to make small
  risk-officer logic changes before cohort samples accumulate). Explicitly
  preserved the existing Phase-0 kill-criterion discipline: detector
  diversification and primary-source deep-reading (the two biggest
  offense levers) stay gated behind the 50-evaluated-candidates checkpoint
  already in ROADMAP.md — this audit does not override that. Nothing in
  the backlog has been implemented; awaiting user go-ahead per tier.
- **2026-07-14(b)** User flagged that development had proceeded feature-by-
  feature with no single presentation of the full picture (To-Be
  architecture, As-Is gap, investment principle, data model, user
  workflows) — described feeling unable to oversee direction. Wrote
  `docs/design/MASTER_OVERVIEW.ja.md`: requirements recap, honest explanation of
  *why* the design should improve returns (bias removal via role
  separation, asymmetric R:R enforcement, EV hurdle separating "good
  story" from "good trade", mechanical candidate sourcing, calibration/
  skill-vs-luck as the real compounding edge — explicitly framed as "a
  system that promises a guaranteed return is a red flag, not a feature"),
  full To-Be vs As-Is gap table, ER diagram, two sequence diagrams, and
  per-task user workflows (candidate selection / holding / post-sale /
  system improvement / retrospective). Published as an artifact too.
  Added the governance rule above per the user's explicit request.
- **2026-07-14** First real (non-synthetic) session-mode run: 3/3 scouted
  candidates PASSed, all via Judge rule enforcement (unaddressed severity-5
  attacks / sucker-test failures) — a healthy outcome, but user correctly
  flagged that Bull/Adversary were arguing over news headlines+summaries
  only, with no structured fundamentals. Risk: can't distinguish "process
  correctly rejecting weak setups" from "process structurally incapable of
  ever producing BUY because inputs are too thin" — exactly the H1/H2
  ambiguity the Phase 0 kill criterion exists to catch. Response:
  (1) wired scout's computed eps/revenue surprise into MarketSnapshot as
  structured fields (previously only in catalyst.description prose);
  (2) added InsiderActivity (net open-market buy/sell, Finnhub
  insider-transactions) and AnalystTrend (recommendation counts,
  Finnhub recommendation) to CandidateBrief, wired via duck-typed
  provider.insider_activity()/analyst_trend() (optional — Yahoo-only
  providers degrade to None, never raise); both may require a paid
  Finnhub tier, undocumented which — code treats absence as unverified,
  not "no activity". Prompts updated to cite these fields and to trust
  structured surprise numbers over prose-implied ones. (3) Added
  `hawkeye review-passes` — individual postmortem flagging PASSed/declined
  tickers that moved >= threshold afterward, distinct from `benchmark`'s
  aggregate cohort stats; a big rally on a PASSed name is a signal the
  PASS call may have been wrong (or new info emerged after — check
  `hawkeye show` before concluding either way). 14 new tests (72 total).
- **2026-07-13(b)** Session mode: user runs Hawkeye inside Claude Code on
  subscription (no metered API key). Added `casefile` (case open/step/submit
  CLI) + `/hawkeye-run` skill; API and session drivers share
  `assemble_recommendation()` so records are identical and comparable
  (`model` field distinguishes engines). Scout's earnings-only coverage gap
  acknowledged: off-season quiet is BY DESIGN for Phase 0 (no catalyst = no
  trade); additional detectors (insider clusters, news triage — cheap in
  session mode) queued behind the Phase 0 viability verdict.
- **2026-07-13** Reprioritized on user direction: the full funnel (scout →
  tribunal → monitor → attribution) must be validated BEFORE any automation.
  Added `scout` (Finnhub earnings-calendar surprise screen → gates → ranked
  shortlist, funnel counts persisted in `scans`) and `benchmark` (forward
  returns of BUY vs tribunal-PASS vs gate-reject cohorts — the Phase 0
  viability metric; BUYs must beat the reject pile). ROADMAP.md rewritten
  with measurable phase gates + a project-level kill criterion. Manual
  `evaluate` candidates stay a separate cohort (catalyst.source) so human
  picks never contaminate system-validation stats.
- **2026-07-12** Initial build: contracts, gates, tribunal (Bull/Adversary/
  Judge with rule enforcement), risk officer, hash-chained ledger,
  sentinel, JA reports, CLI, 44 offline tests. Doctrine v1: risk 0.75%/pos,
  RR≥2, EV≥5%, time stop 45d, thesis-accuracy threshold 0.6.
