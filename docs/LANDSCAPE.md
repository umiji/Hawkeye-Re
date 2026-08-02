# Landscape: open-source stock-investment tooling

Survey date: 2026-08-02. Purpose: know what already exists before we build
more of it, and identify components Hawkeye can borrow instead of writing.

Star counts are as observed on the survey date and move fast — treat them as
a popularity signal, not a quality signal. Nothing here is endorsed; most of
these projects have no track record and several are explicitly toys.

## 1. How this survey reads "close to Hawkeye"

Hawkeye's differentiators, in order of how rare they are in the wild:

| # | Hawkeye property | How common elsewhere |
|---|---|---|
| A | Pre-registered, immutable, hash-chained recommendation ledger | Essentially absent |
| B | Probability claims that are Brier-scored → calibration drift visible | Essentially absent |
| C | Outcome attribution 2×2 (skill_win / lucky_win / unlucky_loss / deserved_loss) | Absent |
| D | Code mechanically overturns the LLM (judge rules, risk vetoes) | Rare |
| E | Information separation between roles (Bull never sees attacks) | Rare |
| F | Adversarial Bull/Bear/Judge debate | Very common |
| G | Deterministic pre-LLM gates | Somewhat common |
| H | No autonomous execution, by design | Uncommon (most projects race to execution) |

The market has converged hard on **F** (debate) and has almost nothing on
**A–C** (the epistemic accounting). That is the defensible part of Hawkeye,
and this survey did not turn up a project that does it.

## 2. Closest in concept

### TradingAgents — `TauricResearch/TradingAgents` (~95.3k★, Apache-2.0)
The reference implementation of the trading-firm-as-multi-agent idea, and the
paper (arXiv 2412.20138) most of the field cites. Roles: Fundamentals /
Sentiment / News / Technical analysts → Bull vs Bear researcher debate →
Trader → Risk management + Portfolio manager. Has backtesting with date
fidelity, and a memory layer that injects realised-return lessons from prior
same-ticker and cross-ticker decisions into later runs.

Overlap with Hawkeye: **F**, partially **D** (risk manager can reject).
Missing: pre-registration, calibration scoring, information separation
(the debate is shared context by construction), attribution.
Its reflection loop is the closest thing to our ledger — but it feeds
lessons back into prompts rather than scoring the forecaster.

Worth reading for: the role decomposition and the memory/reflection design.

### orallexa-ai-trading-agent — `alex-jb/orallexa-ai-trading-agent` (~59★, MIT)
Small but startlingly on-theme: **Bull/Bear/Judge debate on Claude Opus**,
Kelly + ATR position sizing, a portfolio-manager gate for concentration and
sector limits, and — unusually — a **public per-source accuracy ledger in
JSONL** with a bias tracker that corrects systematic overconfidence, plus
walk-forward evaluation reporting Sharpe and p-values across 70
strategy-ticker pairs (self-reported: 1 strong pass, 7 pass, 33 marginal).

Overlap: **F**, **D**, and a genuine gesture at **B**. The source-accuracy
ledger is the only calibration-adjacent implementation this survey found.
Missing: immutability/hash-chaining, thesis pre-registration, attribution.
Note `reunios2024/cortex-sentinel-trading-nexus` (~154★) carries a
near-identical description — likely the same codebase under another name;
diff them before trusting either.

Worth reading for: the JSONL source-accuracy ledger and the honest
walk-forward result table (the marginal-heavy result is itself informative).

### claude-trading-skills — `tradermonty/claude-trading-skills` (~2.5k★, MIT)
70+ Claude Skills for US equities, and the closest thing to Hawkeye in
*delivery model* — it runs inside Claude Code/Claude.ai on a subscription,
same as our session mode. Explicit philosophy: "structure decision-making,
not automate trades" (= our **H**). Categories include market regime (12),
core portfolio (6), swing opportunities (8, incl. VCP/CANSLIM/momentum
burst), **trade planning (7, incl. position sizing, discipline gates,
circuit breakers)**, **trade memory (5: journal, postmortem, performance
coaching)**, strategy research (12, incl. backtesting), and advanced
satellite (6, incl. earnings-trade analysis and institutional flow).
Data: Financial Modeling Prep free tier (250 req/day), yfinance, Alpaca
(paper), optional FINVIZ Elite.

Overlap: **H**, partial **G** (discipline gates), partial **C** (postmortem).
Missing: the gates are prompt-level, not code-enforced — which is exactly
our invariant 3. But this is the single most directly comparable project,
and the one most worth reading in full.

### FinRobot — `AI4Finance-Foundation/FinRobot` (~6.7k★)
Lead agent orchestrating specialised equity-research agents; positions
itself on *auditability* and modularity. Closer to research-report
generation than to trade decisions.

### AlphaAnalyst — `kbhujbal/AlphaAnalyst-open-source-autonomous-equity-research-agent` (~45★)
Ticker → analyst-grade memo with DCF, comps, news sentiment, earnings-call
tone. Notable for two things we care about: an explicit **GPT-4o
devil's-advocate pass against a Claude primary** (cross-model adversary —
an idea we have not tried), and **strict citation validation** to prevent
hallucinated numbers.

### ai-hedge-fund — `virattt/ai-hedge-fund` (~62.6k★)
Most-starred project in the space. 14 agents, mostly *investor personas*
(Buffett, Munger, Wood) plus valuation/sentiment/fundamentals/technicals,
feeding a risk manager (position limits) and a portfolio manager (final
orders). Has a backtester. Requires a Financial Datasets API key.
README is emphatic: "educational and research purposes only… not intended
for real trading."

Relevance to us is mostly negative: persona-imitation is the opposite of
Hawkeye's stance (we score *our own* calibration rather than roleplay
someone else's, and personas are unfalsifiable). Useful as a reminder of
what popularity in this space actually rewards.

## 3. Platforms and data layers (candidates to borrow)

### global-stock-data — `simonlin1212/global-stock-data` (~1.4k★, Apache-2.0)
**The most directly actionable finding for Hawkeye.** US market data for AI
coding assistants, packaged as a `SKILL.md` (markdown + embedded Python,
`requests` only), **zero-auth**, 11 sources across 13 layers. Critically it
covers, from free official sources, three of the four gaps
`docs/DATA_SOURCES.md` lists as roadmap items:

- **CBOE options with full Greeks + 0DTE flow** → our "options flow" gap
- **FINRA market-wide short volume** → our "short interest" gap
- **SEC EDGAR filing stream** (incl. insider Form 4) → feeds the insider-cluster
  detector queued behind the Phase 0 verdict
- plus earnings calendar and a market-wide screener (Scout could widen beyond
  Finnhub's earnings-only surface)

It labels every source with a compliance tier: **S** (SEC/Treasury/CFTC —
commercial + redistribution OK), **B** (FINRA — verify before commercial
use), **C** (CBOE/Yahoo/Eastmoney — **personal research only**). That tiering
matters for us: Hawkeye is personal-use, so tier C is fine today, but the
tiers must be recorded if that ever changes.

### OpenBB — `OpenBB-finance/OpenBB` (~71.3k★, AGPLv3)
The serious data-infrastructure play: Open Data Platform (open-source
backend) + Workspace UI + CLI/Python API, exposing the same data to Python,
Excel, REST, and **MCP servers for AI agents**. `pip install openbb`.
Caveat for us: **AGPLv3** — fine for a private personal tool, a licensing
decision if Hawkeye is ever distributed.

### Vibe-Trading — `HKUDS/Vibe-Trading` (~29.2k★, MIT)
Fast-rising (created 2026-04). Agent loop + swarm workers (investment
committee / quant desk / risk committee), **36 MCP tools**, 18 free data
providers (Yahoo, Finnhub, Alpha Vantage, Stooq, SEC EDGAR, CCXT…), nine
regional backtest engines with transaction costs and liquidation modelling,
452 pre-built alphas (Qlib/Alpha101/GTJA191/academic). Ships broker
connectors (Alpaca, Tiger, Longbridge, Futu, OKX, Binance) — i.e. it does
execute, which we do not.
Interesting to us: it advertises **kill switches, mandates, and audit
ledgers** as safety guardrails — the nearest neighbour to our invariants
among the large projects. Worth a look at how those are implemented.

### maverick-mcp — `wshobson/maverick-mcp` (~641★)
Personal stock-analysis MCP server (FastMCP, Tiingo, technical analysis).
A reasonable model if we ever want to expose Hawkeye's `marketdata` layer
to other MCP clients.

### FinanceToolkit — `JerBouma/FinanceToolkit` (~5.2k★)
150+ financial ratios/indicators with **transparent, inspectable formulas**
and an MCP server. Good reference when we want a metric computed the same
way a textbook would.

## 4. Technical analysis → signal timing

| Repo | ★ | Note |
|---|---|---|
| `TA-Lib/ta-lib-python` | 12.2k | The standard. C-backed, needs the native lib. |
| `bukosabino/ta` | 5.1k | Pure pandas/numpy, pip-only — the pragmatic choice for us if we ever expand past our hand-rolled ATR/ADV. |
| `nardew/talipp` | 532 | **Incremental** indicators — O(1) update per new bar rather than recompute. Relevant if Sentinel ever goes intraday. |
| `facioquo/stock-indicators-dotnet` | 1.2k | .NET; unusually well-documented indicator semantics. |
| `white07S/TradingPatternScanner` / `keithorange/PatternPy` | 302 / 469 | Chart-pattern recognition (H&S, wedges, S/R). Low priority — pattern-matching is where discretionary bias re-enters through the back door. |
| `polakowo/vectorbt` | 8.5k | Vectorised backtesting at scale; the tool of choice for sweeping thousands of parameterisations. |
| `edtechre/pybroker` | 3.5k | Backtesting with walk-forward analysis + bootstrapped metrics — the statistically careful option, closest in spirit to our benchmark cohort work. |
| `stefan-jansen/machine-learning-for-trading` | 20.2k | Book code, 3rd ed. The best free curriculum for the statistics we are implicitly relying on. |

## 5. Calibration and forecast accounting (Hawkeye's actual moat)

Nothing in the trading space does this. The closest work lives in the
forecasting community, unconnected to markets:

- `Creneinc/signal-tracker` — prediction tracking + accuracy scoring +
  leaderboards, aimed at holding analysts/CEOs/AI models accountable.
- `yhoiseth/python-prediction-scorer` — Python library implementing proper
  scoring rules (Brier and relatives).
- `jmoral4/superforecastinghelper` — record predictions, compute Brier scores.
- Kalshibench (arXiv 2512.16030) — benchmark for **LLM epistemic calibration
  against prediction markets**. Directly relevant to whether our Bull's
  probabilities are worth Brier-scoring at all; read before Phase 1.

Conclusion: our claims/Brier/attribution stack has no equivalent inside any
trading repo surveyed. If Hawkeye is right about anything, it is that this —
not the debate — is the part worth building.

## 6. Curated lists (for periodic re-survey)

- `georgezouq/awesome-ai-in-finance` — LLM + DL strategies and tools.
- `PlaceNL2026/best-of-algorithmic-trading` and `merovinh/best-of-algorithmic-trading`
  — auto-ranked, updated weekly.
- `vibeyclaw/awesome-sec-filings` — 13F/10-K/10-Q/8-K tooling.

## 7. What we should actually do

Ranked, and deliberately short — none of this precedes the Phase 0 viability
verdict:

1. **Evaluate `global-stock-data` as a Scout/marketdata input.** It closes
   three named gaps in `DATA_SOURCES.md` with zero-auth official sources.
   Respect the compliance tiers; keep the `unverified` discipline.
2. **Read `tradermonty/claude-trading-skills` end to end.** Same delivery
   model (Claude subscription, no metered API), overlapping intent, 70 skills
   of prior art on discipline gates and trade journalling. Cheapest available
   source of ideas for our session mode.
3. **Steal the cross-model adversary idea** from AlphaAnalyst — running the
   Adversary on a different model than the Bull is a real strengthening of
   invariant 4, and session mode makes it nearly free to try.
4. **Look at Vibe-Trading's audit-ledger and kill-switch implementation**
   before we extend our own.
5. **Read Kalshibench** before investing further in Brier scoring — it is the
   only evidence base on whether LLM probability outputs are calibratable.
6. **Do not** adopt investor-persona agents. Unfalsifiable by construction,
   and incompatible with scoring our own calibration.
