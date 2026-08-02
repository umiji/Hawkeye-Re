# Data Sources

Free-tier only (project requirement). Every provider degrades gracefully:
missing data becomes `None` and shows up in gate reports as `unverified` —
never a silent pass.

| Need | Source | Endpoint | Key |
|---|---|---|---|
| Daily OHLCV (1y) | Yahoo Finance | `query1.finance.yahoo.com/v8/finance/chart/{t}` | none |
| News (fallback) | Yahoo Finance | `/v1/finance/search` | none |
| Company profile, market cap, sector | Finnhub | `/stock/profile2` | `FINNHUB_API_KEY` (free) |
| Company news (14d) | Finnhub | `/company-news` | 〃 |
| Next earnings date | Finnhub | `/calendar/earnings` | 〃 |
| Insider open-market buy/sell (net, 90d) | Finnhub | `/stock/insider-transactions` | 〃 (may require a paid tier — degrades to `None`, never a silent zero) |
| Analyst recommendation trend (latest vs. prior period) | Finnhub | `/stock/recommendation` | 〃 (same caveat) |

Composite policy: Yahoo for prices always; Finnhub for profile/news/earnings
when a key is present, Yahoo news otherwise.

Derived indicators (computed locally in `marketdata/snapshot.py`, unit-tested):
20-day average dollar volume · 14-day ATR as % of price · event-day
close-to-close move · change since event · trading days since event ·
52-week high/low.

Manual overrides: every snapshot field can be overridden from the CLI
(`--price`, `--market-cap`, `--adv`, `--atr-pct`, `--gap-pct`,
`--days-since-event`) for offline runs or when a free endpoint is down.

## Dossier contents (what Bull/Adversary actually see)

`CandidateBrief` — the only thing the tribunal reads — carries: the
catalyst description, up to 10 news items (headline + source + summary),
the quantitative snapshot (price/liquidity/volatility/event-reaction
numbers, and structured `eps_surprise_pct`/`revenue_surprise_pct` when the
candidate came from scout), and — when a Finnhub tier supports them —
`insider_activity` (net open-market buy/sell, 90d) and `analyst_trend`
(recommendation counts, latest vs. prior period). It does NOT include
consensus estimate revision *history*, institutional positioning, short
interest, or options flow. A missing `insider_activity`/`analyst_trend`
field means "unverified", never "no activity" — the prompts say this
explicitly so the tribunal doesn't misread silence as a clean signal.

Known free-tier gaps (roadmap): institutional positioning, short interest,
options flow, consensus estimate revision *history* (only the latest
surprise, not the trend of revisions). These currently enter only through
the Adversary's reasoning over news text, not as hard data.
