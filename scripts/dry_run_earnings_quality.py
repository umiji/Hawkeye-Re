"""Live dry run of the three-leg earnings judgment for one ticker.

Deliberately OUTSIDE `tests/`: the offline suite talks to nothing, and this
script exists precisely to talk to Finnhub, EarningsWhispers, Yahoo and
EDGAR. It writes to a
throwaway database (`--db`, default under the system temp directory) so a
rehearsal never touches the real ledger.

What it shows, in this order:

1. every raw number each source returned, side by side — because a
   percentage alone hides which source it came from, and the whole 2026-08-02
   investigation started with a percentage nobody could trace;
2. the three-leg verdict computed from them.

It used to print a third block: EDGAR's filed XBRL figures, used to validate a
reading of the company's release. Both are gone from the system, so both are
gone from here (tests/test_removed_escalations.py).

Guidance still cannot be fetched — no structured source for it exists on any
free tier — so it is supplied via `--guidance FILE`, a small JSON document
produced by reading the company's release. Nothing checks it: with XBRL gone
there is no filed figure to check it against, so what this exercises is the
guidance leg's arithmetic, not the reading's truth.

    python scripts/dry_run_earnings_quality.py AMZN --date 2026-07-31
    python scripts/dry_run_earnings_quality.py AMZN --guidance amzn.json

The JSON shape:

    {"guidance": {"period": "2026-Q3", "eps_low": null, "eps_high": null,
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
    GuidanceReading,
    PrintSource,
    Stock,
)
from hawkeye.envfile import load_local_env                    # noqa: E402
from hawkeye.ledger.stocks import StockStore                  # noqa: E402
from hawkeye.marketdata.consensus import (                    # noqa: E402
    YahooConsensusSource,
    shift_after_print,
)
from hawkeye.marketdata.edgar import EdgarDirectory           # noqa: E402
from hawkeye.marketdata.finnhub import FinnhubProvider        # noqa: E402
from hawkeye.marketdata.whispers import (                     # noqa: E402
    WhispersSource,
    WhispersUnavailable,
    read_consensus,
    read_guidance,
)
from hawkeye.reports.quality_ja import render_quality_ja      # noqa: E402
from hawkeye.scout.earnings import parse_calendar             # noqa: E402
from hawkeye.scout.numbers import read_numbers                # noqa: E402
from hawkeye.scout.quality import (                           # noqa: E402
    assess_earnings,
    print_from_event,
    reconstructed_consensus,
)

class _FeedStub:
    """The record fetched above, handed to production's selection rule.

    A second live request would be a second answer, and the page would then
    explain a reading the code never made.
    """

    def __init__(self, record, error: str) -> None:
        self._record = record
        self._error = error

    def details(self, ticker: str):
        if self._error:
            raise WhispersUnavailable(self._error)
        return self._record


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

    # 2. the earnings feed ---------------------------------------------------
    # 順位付けに使う数値の出所。⚠️ 1つの決算につき提供元は1つ — 実績と
    # コンセンサスは必ず同じ提供元から取る。本番と同じ read_numbers を通す。
    print("\n[2] 決算専門サイト(EarningsWhispers)")
    try:
        record = WhispersSource().details(ticker)
    except WhispersUnavailable as exc:
        record, feed_error = None, str(exc)
    else:
        feed_error = ""
    if record is None:
        print(f"    レコードを取得できませんでした"
              f"{f'({feed_error})' if feed_error else ''}")
    else:
        print(f"    実績 EPS {_fmt(record.eps_actual)}"
              f" / コンセンサス {_fmt(record.eps_consensus)}")
        print(f"    実績 売上 {_fmt(record.revenue_actual)}"
              f" / コンセンサス {_fmt(record.revenue_consensus)}")
        print(f"    四半期 {record.fiscal_quarter or '—'}"
              f" / 発表 {record.announced_at}"
              f" / 取得できなかった項目: {', '.join(record.gaps) or 'なし'}")
        readout = read_guidance(record)
        print(f"    ガイダンス読み取り: "
              f"{readout.reading or readout.reason or '—'}")
        # The full-year yardstick lives in the SAME summary string, and a
        # full-year guidance is unjudgeable without it. Printed beside the
        # guidance so the pair can be checked together.
        bar = read_consensus(record)
        print(f"    通期コンセンサス読み取り: "
              f"{bar.full_year_period or bar.reason or '—'} EPS "
              f"{_fmt(bar.full_year_eps)} / 売上 "
              f"{_fmt(bar.full_year_revenue)}")

    read, stats = read_numbers([event], [], _FeedStub(record, feed_error),
                               limit=1, always=[(event.ticker, event.day)])
    event = read[0]
    print(f"    → 採用した提供元: {event.numbers_source}"
          f"{f' ({event.numbers_reason})' if event.numbers_reason else ''}")

    # 3. Yahoo: ガイダンスの物差しだけ ---------------------------------------
    consensus_reading = YahooConsensusSource().consensus(ticker)
    print("\n[3] Yahoo(ガイダンスの比較対象としてのみ使用)")
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

    # 4. guidance, read by hand or by an agent ------------------------------
    directory = EdgarDirectory()
    cik = directory.cik_for(ticker)
    extracted = _load_guidance(args.guidance)
    guidance = None
    print("\n[4] 決算発表文からの読み取り(ガイダンスの手入力)")
    if not extracted:
        print("    (--guidance が指定されていないため、ガイダンスは"
              "不明のままです)")
    else:
        payload = extracted.get("guidance") or {}
        if payload:
            guidance = GuidanceReading(**payload)
        if guidance:
            print(f"    ガイダンス {guidance.period}: EPS "
                  f"{_fmt(guidance.eps_low)}〜{_fmt(guidance.eps_high)} / 売上 "
                  f"{_fmt(guidance.revenue_low)}〜{_fmt(guidance.revenue_high)}")
        print("    ※ この読み取りを検算する手段はありません(EDGARのXBRL照合を"
              "廃止したため)。誤読はそのまま判定に入ります。")

    # 5. store and judge ----------------------------------------------------
    store = StockStore(db)
    stock_id = store.put_stock(Stock(cik=cik, ticker=ticker,
                                     name=directory.name_for(ticker)))
    # print_from_event already stamps the chosen vendor and carries the feed's
    # guidance; only a hand-supplied reading overrides it here.
    row = print_from_event(event, stock_id)
    if guidance is not None:
        row = row.model_copy(update={"guidance": guidance})
    if row.reported_at is None:
        row = row.model_copy(update={
            "reported_at": datetime.combine(event.day, datetime.min.time())})

    # The reported quarter's consensus comes from the print itself, from
    # whichever vendor supplied the actual. The forward endpoint read AFTER
    # the release is one quarter out — its "this quarter" is the quarter now
    # in progress — so it contributes ONLY the guidance yardstick.
    snapshot = reconstructed_consensus(event, stock_id, row.fiscal_quarter)
    if consensus_reading is not None:
        shifted = shift_after_print(consensus_reading)
        snapshot = snapshot.model_copy(update={
            "next_quarter_eps_avg": shifted.next_quarter_eps_avg,
            "next_quarter_revenue_avg": shifted.next_quarter_revenue_avg})
    snapshot_id = store.capture_consensus(snapshot)
    stored = store.consensus(snapshot_id)
    if store.active_print(stock_id, row.fiscal_quarter) is None:
        store.record_print(row.model_copy(
            update={"consensus_snapshot_id": snapshot_id}))

    quality = assess_earnings(row, stored, config)
    print("\n[4] 3本柱の判定")
    print(render_quality_ja(quality))
    print(f"\n(記録先: {db} — 本番の台帳ではありません)")
    print(f" hawkeye 相当の順位付けスコア: {quality.score}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
