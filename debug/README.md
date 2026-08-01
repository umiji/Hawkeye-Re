# debug/ — market-data probe UI

A local page that shows, for one ticker, **the raw responses Hawkeye
receives from the market-data APIs beside the reading the production code
makes of them.**

It exists because the defects found on 2026-08-01 were invisible from the
CLI: the earnings calendar returns several rows for a single print with
different consensus figures, and for AAPL it returned an EPS actual
belonging to the *previous* quarter on a row whose revenue was current.
The screen collapses and normalises before anything is rendered, so by the
time a number reaches a report, the disagreement that produced it is gone.

## Running it

```bash
.venv/Scripts/python.exe debug/server.py        # Windows
.venv/bin/python debug/server.py                # elsewhere
```

Opens <http://127.0.0.1:8765/>. Type a ticker, press 取得. `?ticker=` is
mirrored into the URL hash, so `http://127.0.0.1:8765/#AAPL` loads
directly and a link is shareable between sessions.

Flags: `--port`, `--host`, `--no-open`.

Start it from the repository root — `FINNHUB_API_KEY` is read from
`.env.local` there, not from the shell.

## Why a server rather than a bare HTML file

The API key must not reach the browser, and Finnhub rejects
browser-origin requests anyway. The page asks this process; this process
asks Finnhub. It binds the loopback interface, and the recorded request
log deliberately omits the token.

## What it shows

| Panel | Endpoint | Note |
| --- | --- | --- |
| 株価 | Yahoo `/v8/finance/chart` | Finnhub's free tier serves no candles — `FinnhubProvider.daily_history` returns `[]` by design |
| 決算カレンダー | Finnhub `/calendar/earnings` | the hero panel: every raw row per print, with disagreeing fields highlighted |
| 企業プロフィール | Finnhub `/stock/profile2` + `/calendar/earnings` | market cap is rescaled from Finnhub's millions |
| インサイダー売買 | Finnhub `/stock/insider-transactions` | absent = unverified, never "no activity" |
| アナリスト推奨 | Finnhub `/stock/recommendation` | same |
| ニュース | Finnhub `/company-news` | anchored on the latest print, as the scout does |
| HTTPリクエスト一覧 | — | every call with status and duration, so a 403 is never mistaken for an empty result |

## Two rules this tool must keep

1. **The raw payload is captured, not re-fetched.** `_RecordingFinnhub`
   subclasses the real provider and records at its single HTTP chokepoint,
   so what is displayed is literally the response Hawkeye received.
2. **The interpretation is the production code's.** Surprise percentages,
   duplicate-row collapsing, trust flags, the screen verdict and the
   ranking score all come from `hawkeye.scout.earnings` and
   `hawkeye.marketdata.snapshot`. A debug view that drifts from the engine
   it explains is worse than none.

`tests/test_debug_probe.py` pins both (offline).

## Known limitation of the source

Scoped to a symbol, Finnhub returns only **the most recent print plus
upcoming scheduled dates**, however far back `from` is set (measured
2026-08-01). Comparing several past quarters for one ticker is therefore
not possible through this endpoint.

## Scope

Developer tooling. Not part of the installed wheel, and nothing under
`hawkeye/` may import from here.
