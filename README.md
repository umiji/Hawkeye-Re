# Hawkeye

Adversarial-verification investment decision system for catalyst-driven US
equities (multi-week holding period MVP).

## Core hypothesis

A mechanical process that (a) pre-registers every falsifiable claim before
money moves, (b) subjects every idea to a structured adversarial attack, and
(c) scores itself against reality afterwards, will make higher-quality
investment decisions than a human team — because it cannot fall in love with
a position. This project exists to test that hypothesis; the audit trail it
accumulates is a first-class deliverable, win or lose.

Target: 50% annualized return. Honest math: that is ~3.4%/month compounded.
With up to 8 concurrent positions and ~4-week holds, the book needs roughly
+1.7% per position-month net — achievable only with strict asymmetry
(reward/risk ≥ 2), ruthless kill criteria, and zero tolerance for thesis
drift. The doctrine encodes exactly that. See docs/INVESTMENT_DOCTRINE.md.

## How a decision is made

```
market data ──> CandidateBrief (facts only)
                     │
             [Entry Gates]  deterministic; hard fail = PASS, zero LLM spend
                     │
             [Bull]         strongest honest long case; falsifiable claims
                     │       with probabilities + deadlines; kill criteria
             [Adversary]    systematic attack across a fixed taxonomy;
                     │       "sucker test"; strongest short case
             [Judge]        BUY/PASS under pre-registered rules; every
                     │       severe attack must be addressed (code-enforced)
             [Risk Officer] deterministic sizing; veto on reward/risk & EV
                     │
             append-only hash-chained ledger  ──>  Japanese report to user
                     │
             user decides Yes/No and executes the trade themselves
                     │
             sentinel checks kill criteria daily; claims resolve at horizon;
             outcomes classified skill_win / lucky_win / unlucky_loss /
             deserved_loss; Brier scores measure calibration
```

Hawkeye never places orders. It recommends, records, and audits.

## Two ways to drive the LLM roles

| Mode | LLM engine | Cost | How |
|---|---|---|---|
| **Session mode** (recommended) | Your Claude Code session — Bull/Adversary/Judge run as isolated subagents | Claude subscription, no API key | Open this repo in Claude Code and run `/hawkeye-run` |
| API mode | Anthropic API (`claude-opus-4-8`) | Metered API key | `hawkeye evaluate` / `hawkeye scout --evaluate N` |

Both modes share the identical deterministic tail (parsers → judge-rule
enforcement → risk-officer veto → ledger), implemented once in
`assemble_recommendation()` — the records they produce are directly
comparable, and the `model` field says which engine argued the case.
In session mode the CLI's `case step` command is the information-separation
boundary: it emits only what the next role is allowed to see, so the
orchestrating session cannot leak the attacks to the Bull even by accident.

## Quickstart (session mode)

```bash
export FINNHUB_API_KEY=...       # free key; needed by scout
# open this repo in Claude Code, then:
/hawkeye-run
```

## Quickstart (API mode / standalone CLI)

```bash
pip install -e ".[llm]"          # or: uv pip install -e ".[llm]"
export ANTHROPIC_API_KEY=...     # or `ant auth login`
export FINNHUB_API_KEY=...       # optional; enriches profile/news/earnings

# Evaluate one candidate through the full tribunal
hawkeye evaluate NVDA \
  --catalyst earnings_beat_raise \
  --description "Q2 beat, FY guidance raised 8%" \
  --event-date 2026-07-08 --nav 100000

# Record your decision and fills
hawkeye decide rec_xxxx --yes
hawkeye record-entry rec_xxxx --price 182.50 --shares 40 --date 2026-07-14

# Daily
hawkeye check                    # sentinel sweep of open positions

# On exit
hawkeye close rec_xxxx --price 205.00 --date 2026-08-02
hawkeye claims rec_xxxx          # list pre-registered claims
hawkeye resolve-claim rec_xxxx clm_yyyy --true
hawkeye outcome rec_xxxx         # P&L + skill-vs-luck attribution

# Anytime
hawkeye calibration              # book-level Brier / quadrant stats
hawkeye verify                   # ledger hash-chain integrity
```

Runs without any API key for everything except `evaluate` (LLM) — tests and
the deterministic core are fully offline.

## Documentation

| Doc | Contents |
|---|---|
| [docs/MASTER_OVERVIEW.ja.md](docs/MASTER_OVERVIEW.ja.md) | **起点はここから。** To-Be全体像・As-Is差分・投資原則・ER図・シーケンス・Userワークフロー(日本語) |
| [docs/STRATEGY_BACKLOG.ja.md](docs/STRATEGY_BACKLOG.ja.md) | 「50%必達」観点での戦略・戦術レビューと優先順位付きバックログ(日本語) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Service decomposition, contracts, data flow |
| [docs/INVESTMENT_DOCTRINE.md](docs/INVESTMENT_DOCTRINE.md) | Strategy, gates, sizing, base rates — every number pre-registered |
| [docs/VERIFICATION_PROTOCOL.md](docs/VERIFICATION_PROTOCOL.md) | The adversarial process spec and its bias-elimination mechanisms |
| [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | Free-tier data sources and degradation behavior |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Path from manual MVP to automated daily operation |
| [docs/USER_GUIDE.ja.md](docs/USER_GUIDE.ja.md) | 日本語ユーザーガイド(日々の運用手順) |

System documentation is English (token economy); all user-facing output —
reports and the user guide — is Japanese.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest
```
