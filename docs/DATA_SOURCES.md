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

Composite policy: Yahoo for prices always; Finnhub for profile/news/earnings
when a key is present, Yahoo news otherwise.

Derived indicators (computed locally in `marketdata/snapshot.py`, unit-tested):
20-day average dollar volume · 14-day ATR as % of price · event-day
close-to-close move · change since event · trading days since event ·
52-week high/low.

Manual overrides: every snapshot field can be overridden from the CLI
(`--price`, `--market-cap`, `--adv`, `--atr-pct`, `--gap-pct`,
`--days-since-event`) for offline runs or when a free endpoint is down.

Known free-tier gaps (roadmap): institutional positioning, short interest,
options flow, consensus estimate revisions. These currently enter only
through the Adversary's reasoning, not as hard data.
