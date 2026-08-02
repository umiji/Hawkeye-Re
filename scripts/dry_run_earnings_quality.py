"""Live dry run of the three-leg earnings judgment for one ticker.

Deliberately OUTSIDE `tests/`: the offline suite talks to nothing, and this
script exists precisely to talk to Finnhub, Yahoo and EDGAR. It writes to a
throwaway database (`--db`, default under the system temp directory) so a
rehearsal never touches the real ledger.

What it shows, in this order:

1. every raw number each source returned, side by side — because a
   percentage alone hides which source it came from, and the whole 2026-08-02
   investigation started with a percentage nobody could trace;
2. the EDGAR XBRL figures, which are the only ones with a primary source;
3. the three-leg verdict computed from them.

Guidance and any published non-GAAP figure cannot be fetched — no structured
source for either exists on any free tier — so they are supplied via
`--guidance FILE`, a small JSON document produced by reading the company's
own release. That extraction is not trusted on sight: its GAAP EPS must equal
EDGAR's filed value or the whole document is rejected (§5.3(5)).

    python scripts/dry_run_earnings_quality.py AMZN --date 2026-07-31
    python scripts/dry_run_earnings_quality.py AMZN --guidance amzn.json

The JSON shape:

    {"gaap_eps_diluted": 5.75,
     "non_gaap_eps": null,
     "one_off_per_share": null,
     "one_off_description": "",
     "guidance": {"period": "2026-Q3", "eps_low": null, "eps_high": null,
                  "revenue_low": 1.74e11, "revenue_high": 1.80e11,
                  "source_excerpt": "..."},
     "source_url": "https://www.sec.gov/..."}
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hawkeye.config import HawkeyeConfig                      # noqa: E402
from hawkeye.contracts.stocks import (                        # noqa: E402
    EpsBasis,
    GuidanceReading,
    PrintDepth,
    Stock,
)
from hawkeye.envfile import load_local_env                    # noqa: E402
from hawkeye.ledger.stocks import StockStore                  # noqa: E402
from hawkeye.marketdata.consensus import (                    # noqa: E402
    YahooConsensusSource,
    shift_after_print,
)
from hawkeye.marketdata.edgar import EdgarDirectory           # noqa: E402
from hawkeye.marketdata.edgar_facts import (                  # noqa: E402
    EdgarFacts,
    extraction_matches_filing,
)
from hawkeye.marketdata.finnhub import FinnhubProvider        # noqa: E402
from hawkeye.marketdata.yahoo_earnings import YahooEarningsSource  # noqa: E402
from hawkeye.reports.quality_ja import render_quality_ja      # noqa: E402
from hawkeye.scout.earnings import parse_calendar             # noqa: E402
from hawkeye.scout.quality import (                           # noqa: E402
    assess_earnings,
    print_from_event,
    reconstructed_consensus,
)

_REVENUE_TAGS = ("RevenueFromContractWithCustomerExcludingAssessedTax",
                 "Revenues", "SalesRevenueNet")


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:,.4g}" if abs(value) >= 1000 else f"{value:.4g}"


def _find_event(finnhub: FinnhubProvider, ticker: str,
                report_date: Optional[date]):
    """The print, with every duplicate row the calendar returned for it."""
    # A few days either side of the nominal date: the calendar files a print
    # under the session it belongs to, which is not always the day the wire
    # crossed, and an off-by-one here would read as "no such print".
    end = (report_date + timedelta(days=3)) if report_date else date.today()
    start = end - timedelta(days=6 if report_date else 14)
    raw = [row for row in finnhub.earnings_calendar(start, end)
           if (row.get("symbol") or "").upper() == ticker]
    if not raw:
        return None, []
    events = [e for e in parse_calendar(raw) if e.eps_actual is not None]
    if not events:
        return None, raw
    return max(events, key=lambda e: e.day), raw


def _load_guidance(path: Optional[str]) -> dict:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: Optional[list[str]] = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", nargs="?", default="AMZN")
    parser.add_argument("--date", default=None,
                        help="the print's report date (YYYY-MM-DD)")
    parser.add_argument("--guidance", default=None,
                        help="JSON extracted from the company's release")
    parser.add_argument("--db", default=None,
                        help="throwaway database (default: temp directory)")
    args = parser.parse_args(argv)

    load_local_env()
    config = HawkeyeConfig.from_env()
    ticker = args.ticker.upper()
    report_date = date.fromisoformat(args.date) if args.date else None
    db = args.db or str(Path(tempfile.gettempdir()) / "hawkeye_dryrun.db")

    finnhub = FinnhubProvider()
    if not finnhub.available:
        print("FINNHUB_API_KEY がありません(.env.local を確認してください)",
              file=sys.stderr)
        return 1

    print(f"=== {ticker} 決算品質のドライラン ===\n")

    # 1. the calendar --------------------------------------------------------
    event, raw_rows = _find_event(finnhub, ticker, report_date)
    if event is None:
        print(f"{ticker}: 決算カレンダーに実績のある行が見つかりません")
        return 1
    print(f"[1] Finnhub 決算カレンダー ({len(raw_rows)} 行)")
    for row in raw_rows:
        print(f"    {row.get('date')} Q{row.get('quarter')}/{row.get('year')}"
              f"  EPS実績 {_fmt(row.get('epsActual'))}"
              f" / 予想 {_fmt(row.get('epsEstimate'))}"
              f"  売上実績 {_fmt(row.get('revenueActual'))}"
              f" / 予想 {_fmt(row.get('revenueEstimate'))}")
    print(f"    → 採用した読み: {event.day} {event.fiscal_quarter} "
          f"実績 {_fmt(event.eps_actual)} / 予想 {_fmt(event.eps_estimate)}"
          f"  (同一決算の実績値: "
          f"{', '.join(_fmt(v) for v in event.all_eps_actuals) or '—'})")

    # 2. Yahoo --------------------------------------------------------------
    yahoo_print = YahooEarningsSource().verified_earnings(ticker, event.day)
    consensus_reading = YahooConsensusSource().consensus(ticker)
    print("\n[2] Yahoo")
    if yahoo_print is None:
        print("    実績: 取得できませんでした(未検証)")
    else:
        print(f"    実績 EPS {_fmt(yahoo_print.eps_actual)}"
              f" / 予想 {_fmt(yahoo_print.eps_estimate)}"
              f" / 公表サプライズ {yahoo_print.surprise_pct:+.2f}%")
    if consensus_reading is None:
        print("    コンセンサス: 取得できませんでした")
    else:
        print(f"    進行中の四半期(=ガイダンスの対象) EPS 平均 "
              f"{_fmt(consensus_reading.eps_avg)}"
              f" (下限 {_fmt(consensus_reading.eps_low)} /"
              f" 上限 {_fmt(consensus_reading.eps_high)} /"
              f" アナリスト {consensus_reading.eps_analysts}人)")
        print(f"    進行中の四半期 売上 平均 "
              f"{_fmt(consensus_reading.revenue_avg)}"
              f" (アナリスト {consensus_reading.revenue_analysts}人)")
        print(f"    その次の四半期 EPS "
              f"{_fmt(consensus_reading.next_quarter_eps_avg)} / 売上 "
              f"{_fmt(consensus_reading.next_quarter_revenue_avg)}")
    print("    ※ この予想は決算「後」に取得したものです。Yahooの期ラベルは"
          "「今日から見た四半期」なので、発表済みの決算の予想は含まれません"
          "(AMZNで実測: 0q=1.956 は Q2 の 1.83 ではなく Q3 の予想)。"
          "したがってガイダンスの比較対象としてのみ使い、"
          "発表済み四半期の予想は決算履歴側の値を使います。")

    # 3. EDGAR --------------------------------------------------------------
    directory = EdgarDirectory()
    cik = directory.cik_for(ticker)
    edgar = EdgarFacts()
    xbrl_eps = xbrl_revenue = None
    print("\n[3] EDGAR XBRL")
    if cik is None:
        print("    CIK を特定できませんでした(暫定IDで記録します)")
    else:
        fact = edgar.quarterly(cik, "EarningsPerShareDiluted", event.day)
        xbrl_eps = fact.value if fact else None
        for tag in _REVENUE_TAGS:
            revenue_fact = edgar.quarterly(cik, tag, event.day)
            if revenue_fact is not None:
                xbrl_revenue = revenue_fact.value
                break
        print(f"    CIK {cik} / 希薄化後EPS {_fmt(xbrl_eps)}"
              + (f" ({fact.period_start}〜{fact.period_end}, {fact.form})"
                 if fact else " (該当なし=未検証)"))
        print(f"    売上 {_fmt(xbrl_revenue)}")

    # 4. the release, read by hand or by an agent ---------------------------
    extracted = _load_guidance(args.guidance)
    guidance = None
    basis = EpsBasis.AS_REPORTED
    eps_release = one_off = None
    print("\n[4] 決算発表文からの読み取り")
    if not extracted:
        print("    (--guidance が指定されていないため、ガイダンスと"
              "Non-GAAP EPS は不明のままです)")
        basis = EpsBasis.UNADJUSTED
    elif not extraction_matches_filing(extracted.get("gaap_eps_diluted"),
                                       xbrl_eps):
        print(f"    棄却: 抽出されたGAAP EPS "
              f"{_fmt(extracted.get('gaap_eps_diluted'))} が "
              f"EDGARの {_fmt(xbrl_eps)} と一致しません。"
              f"前年同期の列を読んだ可能性があるため、この抽出は"
              f"まるごと採用しません。")
        basis = EpsBasis.UNADJUSTED
    else:
        payload = extracted.get("guidance") or {}
        if payload:
            guidance = GuidanceReading(**payload)
        eps_release = extracted.get("non_gaap_eps")
        one_off = extracted.get("one_off_per_share")
        if eps_release is not None:
            basis = EpsBasis.ADJUSTED
        elif one_off is not None:
            eps_release = round(extracted["gaap_eps_diluted"] - one_off, 4)
            basis = EpsBasis.ADJUSTED
        else:
            basis = EpsBasis.UNADJUSTED
        print(f"    検算OK (GAAP EPS = EDGAR = {_fmt(xbrl_eps)})")
        print(f"    Non-GAAP EPS {_fmt(extracted.get('non_gaap_eps'))}"
              f" / 一時要因(一株あたり) {_fmt(one_off)}"
              f" {extracted.get('one_off_description', '')}")
        if guidance:
            print(f"    ガイダンス {guidance.period}: EPS "
                  f"{_fmt(guidance.eps_low)}〜{_fmt(guidance.eps_high)} / 売上 "
                  f"{_fmt(guidance.revenue_low)}〜{_fmt(guidance.revenue_high)}")

    # 5. store and judge ----------------------------------------------------
    store = StockStore(db)
    stock_id = store.put_stock(Stock(cik=cik, ticker=ticker,
                                     name=directory.name_for(ticker)))
    row = print_from_event(event, stock_id)
    depth = (PrintDepth.RELEASE_READ if guidance or eps_release
             else PrintDepth.XBRL_VALIDATED if xbrl_eps is not None
             else PrintDepth.VERIFIED if yahoo_print else PrintDepth.CALENDAR_ONLY)
    row = row.model_copy(update={
        "eps_yahoo": yahoo_print.eps_actual if yahoo_print else row.eps_yahoo,
        "eps_xbrl_diluted": xbrl_eps, "revenue_xbrl": xbrl_revenue,
        "eps_release": eps_release, "eps_basis": basis,
        "one_off_per_share": one_off, "guidance": guidance,
        "reported_at": datetime.combine(event.day, datetime.min.time()),
        "depth": depth})

    # The reported quarter's consensus comes from the print itself (Yahoo's
    # earnings history and the calendar). The forward endpoint read AFTER the
    # release is one quarter out — its "this quarter" is the quarter now in
    # progress — so it contributes ONLY the guidance yardstick.
    snapshot = reconstructed_consensus(event, stock_id, row.fiscal_quarter)
    if yahoo_print is not None:
        snapshot = snapshot.model_copy(
            update={"eps_avg": yahoo_print.eps_estimate})
    if consensus_reading is not None:
        shifted = shift_after_print(consensus_reading)
        snapshot = snapshot.model_copy(update={
            "next_quarter_eps_avg": shifted.next_quarter_eps_avg,
            "next_quarter_revenue_avg": shifted.next_quarter_revenue_avg})
    snapshot_id = store.capture_consensus(snapshot)
    stored = store.consensus(snapshot_id)
    if store.latest_print(stock_id, row.fiscal_quarter) is None:
        store.record_print(row.model_copy(
            update={"consensus_snapshot_id": snapshot_id}))

    quality = assess_earnings(row, stored, config)
    print("\n[5] 3本柱の判定")
    print(render_quality_ja(quality))
    print(f"\n(記録先: {db} — 本番の台帳ではありません)")
    print(f" hawkeye 相当の順位付けスコア: {quality.score}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
