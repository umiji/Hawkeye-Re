# Investment Doctrine

Everything numeric here is code (`hawkeye/config.py`), pre-registered and
version-controlled. Changing a rule is a commit, not a mood.

## 1. Mandate (MVP)

- Universe: US-listed common stock, price ≥ $5, market cap ≥ $300M,
  20-day average dollar volume ≥ $10M.
- Style: catalyst-driven, long-only, holding period measured in weeks
  (time stop 45 days). The long-term fundamental book is out of scope until
  the short-cycle machine is validated.
- The system recommends; the user executes. No autonomous trading, ever.

## 2. The math behind the 50% target

50%/year ≈ +3.4%/month compounded. With ≤ 8 concurrent positions sized ≤ 10%
NAV each and ~4-week holds, the book needs ≈ +1.7% per position-month net.
A realistic profile that achieves it:

- hit rate ~55%
- average win ≈ 2 × average loss (enforced: reward/risk ≥ 2 at entry)
- losses truncated by pre-registered stops; winners not cut early by mood
  (exits happen on target, kill, or time stop — not vibes)

This is ambitious, and ambition is not a plan: the honest posture is to run
the process with full discipline, measure relentlessly, and let the ledger
say whether the edge is real — then act on what it says.

What the ledger is for: a shortfall has two very different causes, and only
the record separates them. If the process is calibrated and positive-
expectancy but lands under 50%, the gap is *size* — too few trades, winners
cut too early, too little risk per trade — and those are fixable parameters
(see `strategy/STRATEGY_BACKLOG.ja.md`). If the process is not calibrated,
the edge is not real and no amount of sizing rescues it. Both readings
demand a change. Neither makes a return-less year a success.

## 3. Where the edge is supposed to come from

| Edge type | Mechanism | Canonical setup |
|---|---|---|
| `underreaction` | Post-event drift: analysts and flows reprice slowly | Earnings beat + guidance raise, positive but not euphoric initial move |
| `overreaction` | Forced/emotional selling overshoots fair value | Quality name down hard on a fixable, quantifiable problem |
| `structural_flow` | Mechanical buyers/sellers who don't care about price | Index inclusion, spinoff forced selling |
| `information_synthesis` | Public dots not yet connected by the market | Supplier data implying a beat, filings read closely |

Base rates the tribunal must respect (baked into every prompt):
- PEAD is real but modest — low single-digit percent over 1–3 months.
- Most catalyst trades resolve within ±15% over 4–8 weeks.
- ~Half of well-selected trades lose; the edge is asymmetry + discipline.
- "The market is missing something obvious" in a liquid name is usually false.

## 4. Entry gates (deterministic, pre-LLM)

Hard (fail = automatic PASS, zero LLM spend):
price ≥ $5 · market cap ≥ $300M · ADV20 ≥ $10M · catalyst ≤ 10 trading days old.

Soft (judge must weigh): event-day move ≤ |25%| · ATR14 ≤ 8% of price ·
next earnings not within 7 days. Missing data is flagged `unverified`,
never silently passed.

## 5. Position management

- Risk 0.75% of NAV to the stop; position capped at 10% NAV; ≤ 8 positions.
- Entry requires reward/risk ≥ 2.0 and scenario-weighted EV ≥ +5%
  (computed from the Bull's own pre-registered scenarios — if the Bull's
  numbers don't clear the bar, the Bull's prose cannot save the trade).
- Stops come from the thesis's `price_below` kill criterion (fallback −8%);
  stop wider than 20% is vetoed outright.
- Exits: kill criterion hit (default = exit; holding requires a written
  override), price target reached (take profit or formally re-underwrite),
  time stop, or mandatory review before the next earnings.

## 6. Anti-bias mechanisms (how the returns are protected)

These are not the product — the return is. They exist because every failure
mode below has a measured cost in money: a position held past its stop, a
thesis rewritten after the fact, a lucky win mistaken for a repeatable edge
and then sized up.

| Human failure mode | Mechanical counter |
|---|---|
| Falling in love with a holding | Sentinel compares prices to pre-registered numbers; it has no opinion |
| Moving the goalposts | Claims and kill criteria are pre-registered in an immutable, hash-chained ledger |
| Confusing luck with skill | Every closed trade lands in a 2×2: skill_win / lucky_win / unlucky_loss / deserved_loss |
| Overconfident storytelling | Claims carry probabilities that are Brier-scored; calibration drift is visible in `hawkeye calibration` |
| Polite analysts | The Adversary role exists to say what a colleague wouldn't; the Judge must address every severity ≥ 4 attack in writing or PASS (enforced in code) |
| Sunk-cost creep | Monthly blind re-underwrite (roadmap): would we buy this today, not knowing we own it? |

## 7. Review cadence

- **Daily**: `hawkeye check` — kill criteria, time stops, claim deadlines.
- **Weekly**: P&L plus process health — are gates rejecting everything
  (too tight) or nothing (too loose)? Is the Adversary landing kill-shots?
- **Monthly**: re-underwrite every open position; review calibration table;
  propose doctrine changes as config diffs with rationale.
- **Per closed trade**: resolve claims, compute the outcome quadrant. A
  `lucky_win` triggers the same process review as a `deserved_loss`.
