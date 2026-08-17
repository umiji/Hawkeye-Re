# Data Sources

Free-tier only (project requirement). Every provider degrades gracefully:
missing data becomes `None` and shows up in gate reports as `unverified` —
never a silent pass.

| Need | Source | Endpoint | Key |
|---|---|---|---|
| Daily OHLCV (1y) | Yahoo Finance | `query1.finance.yahoo.com/v8/finance/chart/{t}` | none |
| News (fallback) | Yahoo Finance | `/v1/finance/search` | none |
| Company profile, market cap, sector | Finnhub | `/stock/profile2` | `FINNHUB_API_KEY` (free) |
| Company news (window anchored on the catalyst: `news_lead_days`=3 before, up to `news_max_items`=25 kept) | Finnhub | `/company-news` | 〃 |
| Earnings calendar — WHO reports and WHEN (the scan's whole universe) | Finnhub | `/calendar/earnings` | 〃 |
| **Earnings numbers** — EPS and revenue, actual AND consensus, plus guidance prose and announcement time | **EarningsWhispers** | `earningswhispers.com/api/epsdetails/{t}` | none |
| **Why the quarter came out where it did** — the company's own earnings release, addressed by the `fileName` the row above states | **EarningsWhispers** | `earningswhispers.com/api/newsarticle/{t}/{fileName}` | none |
| Cutting that release to the blocks that explain the quarter, copied verbatim and then checked back against it (T-008) | Google Gemini (`gemini-3.5-flash-lite` since T-011; was `gemini-2.5-flash`, whose free tier is 20 requests/day and could not cover a 30-name scan) | `generativelanguage.googleapis.com` | `GEMINI_API_KEY` (free tier: 500/day, 15/minute; optional — without it the cause step falls back to the summary, which explained 0 of 30) |
| Consensus pre-registration (distribution and analyst count — the only source for either) | Yahoo via yfinance | `earnings_estimate` / `revenue_estimate` | none |
| Ticker → SEC registrant number (CIK), company name | EDGAR | `company_tickers.json` | none |
| Next earnings date | Finnhub | `/calendar/earnings` | 〃 |
| Insider open-market buy/sell (net, 90d) | Finnhub | `/stock/insider-transactions` | 〃 (may require a paid tier — degrades to `None`, never a silent zero) |
| Analyst recommendation trend (latest vs. prior period) | Finnhub | `/stock/recommendation` | 〃 (same caveat) |

Composite policy: Yahoo for prices always; Finnhub for profile, news and the
earnings CALENDAR when a key is present, Yahoo news otherwise.

⚠️ **The earnings calendar and the earnings NUMBERS are different needs with
different sources.** Finnhub says who reports and when; EarningsWhispers
supplies the figures a print is ranked on. One print stands on ONE vendor —
its actual and the consensus it is measured against always come from the same
place, because the feed's consensus is an adjusted-basis figure while the
calendar's actual may be GAAP, and a ratio built from one of each means
nothing (`hawkeye/scout/numbers.py`). When the feed cannot answer, the WHOLE
print falls back to the calendar rather than half of it.

Derived indicators (computed locally in `marketdata/snapshot.py`, unit-tested):
20-day average dollar volume · 14-day ATR as % of price · event-day
close-to-close move · change since event · trading days since event ·
52-week high/low.

Manual overrides: every snapshot field can be overridden from the CLI
(`--price`, `--market-cap`, `--adv`, `--atr-pct`, `--gap-pct`,
`--days-since-event`) for offline runs or when a free endpoint is down.

## Dossier contents (what Bull/Adversary actually see)

`CandidateBrief` — the only thing the tribunal reads — carries: the
catalyst description, up to `news_max_items` (25) news items nearest the
catalyst (headline + source + summary),
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
