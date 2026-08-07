"""Storage for the stock master, its earnings prints, and consensus
snapshots (docs/design/MASTER_OVERVIEW.ja.md §6.1).

Additive: these tables live in the same SQLite file as the decision ledger
but are not part of its hash chain — `Ledger.verify_chain()` is unaffected by
their presence. They are not the record of truth. The ledger is; the master
is a projection that `rebuild_projection()` can recreate from it.

The append-only guarantee is enforced twice on purpose. This class exposes no
method whose name mutates a captured row, AND SQLite triggers abort any
UPDATE or DELETE against `consensus_snapshots` / `earnings_prints`. One
without the other is a convention: the whole reason a decision may reference
these rows by id, instead of copying their numbers into its payload, is that
the rows cannot move under it (invariant 1).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Optional

from hawkeye.contracts.models import to_jst
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    ReviewStage,
    SnapshotKind,
    Stock,
)
from hawkeye.ledger.store import _instant

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    id      TEXT PRIMARY KEY,
    cik     TEXT,
    ticker  TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stocks_ticker ON stocks (ticker);

CREATE TABLE IF NOT EXISTS consensus_snapshots (
    id              TEXT PRIMARY KEY,
    stock_id        TEXT NOT NULL,
    fiscal_quarter  TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consensus_term
    ON consensus_snapshots (stock_id, fiscal_quarter);

CREATE TABLE IF NOT EXISTS earnings_prints (
    id                    TEXT PRIMARY KEY,
    stock_id              TEXT NOT NULL,
    fiscal_quarter        TEXT NOT NULL,
    report_date           TEXT NOT NULL,
    depth                 TEXT NOT NULL,
    consensus_snapshot_id TEXT NOT NULL DEFAULT '',
    payload               TEXT NOT NULL
);
-- A quarter deepens by APPENDING a row per depth. The unique index refuses a
-- second reading at the same depth, which would be a re-measurement of the
-- same evidence, and the triggers below refuse rewriting one in place.
CREATE UNIQUE INDEX IF NOT EXISTS idx_print_depth
    ON earnings_prints (stock_id, fiscal_quarter, depth);
CREATE INDEX IF NOT EXISTS idx_print_stock ON earnings_prints (stock_id);

CREATE TRIGGER IF NOT EXISTS consensus_snapshots_no_update
BEFORE UPDATE ON consensus_snapshots BEGIN
    SELECT RAISE(ABORT, 'consensus_snapshots is append-only: decisions reference these rows by id, so changing one silently rewrites a pre-registered recommendation');
END;
CREATE TRIGGER IF NOT EXISTS consensus_snapshots_no_delete
BEFORE DELETE ON consensus_snapshots BEGIN
    SELECT RAISE(ABORT, 'consensus_snapshots is append-only');
END;
CREATE TRIGGER IF NOT EXISTS earnings_prints_no_update
BEFORE UPDATE ON earnings_prints BEGIN
    SELECT RAISE(ABORT, 'earnings_prints is append-only: deepen by recording a new row at the deeper depth');
END;
CREATE TRIGGER IF NOT EXISTS earnings_prints_no_delete
BEFORE DELETE ON earnings_prints BEGIN
    SELECT RAISE(ABORT, 'earnings_prints is append-only');
END;
"""


@dataclass(frozen=True)
class StockHistory:
    """Everything the system knows about one company, in one read — the
    question the old ticker-as-a-string layout could only answer by scanning
    every decision record."""
    stock: Stock
    prints: list[EarningsPrint]                     # deepest row per quarter
    consensus: dict[str, ConsensusSnapshot]         # quarter -> frozen row
    decisions: list[dict]
    screened: list[dict]

    def consensus_for(self, fiscal_quarter: str) -> Optional[ConsensusSnapshot]:
        return self.consensus.get(fiscal_quarter)


class StockStore:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._add_ticker_columns()

    def _add_ticker_columns(self) -> None:
        """Expose the ticker as a column on the two payload-keyed tables.

        A VIRTUAL generated column, not a stored one: it needs no back-fill,
        cannot drift from the payload it is computed from, and writes not one
        byte to any recorded row — which matters here, because the
        append-only triggers would refuse an UPDATE anyway. Purely for
        reading: everything in the code goes through the payload.
        """
        if sqlite3.sqlite_version_info < (3, 31):
            print("SQLite が古いため、決算・コンセンサスの表に ticker 列を"
                  "追加できません(読みやすさのためだけの列なので、動作には"
                  "影響しません)", file=sys.stderr)
            return
        for table in ("earnings_prints", "consensus_snapshots"):
            # table_xinfo, not table_info: a generated column is hidden and
            # does not appear in the latter, so the check would say "absent"
            # on every open and the ALTER would fail on the second one.
            columns = {row[1] for row in
                       self._conn.execute(f"PRAGMA table_xinfo({table})")}
            if "ticker" in columns:
                continue
            self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN ticker TEXT GENERATED "
                f"ALWAYS AS (json_extract(payload, '$.ticker')) VIRTUAL")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- master --------------------------------------------------------------

    def put_stock(self, stock: Stock) -> str:
        """Record or refresh the slow-moving attributes; returns the id.

        The review projection is never clobbered by a refresh — attribute
        upkeep and "what did we do about this company" are different facts
        arriving from different places.
        """
        existing = self.stock(stock.id)
        if existing is not None:
            stock = stock.model_copy(update={
                "last_reviewed_fiscal_quarter":
                    existing.last_reviewed_fiscal_quarter,
                "last_reviewed_at": existing.last_reviewed_at,
                "last_stage_reached": existing.last_stage_reached,
                # Same reasoning, and load-bearing: the master row is re-put
                # on every sighting, so a refresh that cleared the triage
                # would make the filter hold for exactly one run.
                "investigation_target": existing.investigation_target,
                "investigation_reason": existing.investigation_reason,
                "investigation_checked_at": existing.investigation_checked_at})
        self._conn.execute(
            "INSERT INTO stocks (id, cik, ticker, payload) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET cik = excluded.cik,"
            " ticker = excluded.ticker, payload = excluded.payload",
            (stock.id, stock.cik, stock.ticker, stock.model_dump_json()))
        self._conn.commit()
        return stock.id

    def stock(self, stock_id: str) -> Optional[Stock]:
        row = self._conn.execute("SELECT payload FROM stocks WHERE id = ?",
                                 (stock_id,)).fetchone()
        return Stock.model_validate_json(row[0]) if row else None

    def stock_by_ticker(self, ticker: str) -> Optional[Stock]:
        """The listed company currently using this ticker.

        A delisted namesake is skipped rather than returned, because the only
        caller that asks by ticker is looking at today's calendar.
        """
        rows = self._conn.execute(
            "SELECT payload FROM stocks WHERE ticker = ?",
            (ticker.strip().upper(),)).fetchall()
        stocks = [Stock.model_validate_json(r[0]) for r in rows]
        listed = [s for s in stocks if s.listing_status == "listed"]
        return (listed or stocks or [None])[0]

    def stocks(self) -> list[Stock]:
        return [Stock.model_validate_json(r[0]) for r in
                self._conn.execute("SELECT payload FROM stocks ORDER BY ticker")]

    def record_review(self, stock_id: str, fiscal_quarter: str,
                      stage: ReviewStage,
                      reviewed_at: Optional[datetime] = None) -> None:
        """Project "we looked at this quarter, and got this far" onto the
        master. A projection, not a fact of record — the ledger holds that."""
        stock = self.stock(stock_id)
        if stock is None:
            return
        updated = stock.model_copy(update={
            "last_reviewed_fiscal_quarter": fiscal_quarter,
            "last_reviewed_at": reviewed_at or stock.as_of,
            "last_stage_reached": stage})
        self._conn.execute("UPDATE stocks SET payload = ? WHERE id = ?",
                           (updated.model_dump_json(), stock_id))
        self._conn.commit()

    def record_triage(self, stock_id: str, is_target: bool, reason: str,
                      on: Optional[date] = None) -> None:
        """Record whether this company is worth spending lookups on
        (§6.1(E)). A projection like the review one — the entry gates in the
        ledger are what it is derived from, and it can be recomputed."""
        stock = self.stock(stock_id)
        if stock is None:
            return
        checked = datetime.combine(on or date.today(), time.min,
                                   tzinfo=timezone.utc)
        updated = stock.model_copy(update={
            "investigation_target": is_target,
            "investigation_reason": reason,
            "investigation_checked_at": checked})
        self._conn.execute("UPDATE stocks SET payload = ? WHERE id = ?",
                           (updated.model_dump_json(), stock_id))
        self._conn.commit()

    def already_reviewed(self, stock_id: str, fiscal_quarter: str) -> bool:
        """Whether this quarter's print has already been through the funnel —
        the one thing the projection exists to answer cheaply."""
        stock = self.stock(stock_id)
        return bool(stock
                    and stock.last_reviewed_fiscal_quarter == fiscal_quarter)

    # -- consensus (append-only) ---------------------------------------------

    def capture_consensus(self, snapshot: ConsensusSnapshot) -> str:
        """Store a capture unless it repeats the newest row verbatim.

        Returns the id of the row now in force — the existing one when
        nothing moved. Callers store THAT id, so a no-op capture leaves every
        existing reference pointing at the same numbers.
        """
        newest = self._newest_consensus(snapshot.stock_id,
                                        snapshot.fiscal_quarter)
        if newest is not None and newest.content_key() == snapshot.content_key():
            return newest.id
        self._conn.execute(
            "INSERT INTO consensus_snapshots"
            " (id, stock_id, fiscal_quarter, captured_at, kind, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (snapshot.id, snapshot.stock_id, snapshot.fiscal_quarter,
             snapshot.captured_at.isoformat(), snapshot.kind.value,
             snapshot.model_dump_json()))
        self._conn.commit()
        return snapshot.id

    def last_pre_registration_at(self) -> Optional[datetime]:
        """When consensus was last pre-registered, or None.

        Read off the snapshots themselves rather than from a run log: the
        rows ARE the record of the run. Reconstructions are excluded because
        the funnel writes those AFTER a print, so counting them would report
        a capture run that never happened.

        A run that captured nothing (every estimate unchanged) writes no row
        and so leaves this where it was — which errs toward treating the next
        run as "after a gap". That is the safe direction: a snapshot missed
        before a print can never be taken, and a redundant lookup costs one
        API call.
        """
        rows = self._conn.execute(
            "SELECT captured_at FROM consensus_snapshots WHERE kind = ?",
            (SnapshotKind.PRE_REGISTERED.value,)).fetchall()
        instants = [_instant(r[0]) for r in rows]
        return max(instants) if instants else None

    def consensus(self, snapshot_id: str) -> Optional[ConsensusSnapshot]:
        row = self._conn.execute(
            "SELECT payload FROM consensus_snapshots WHERE id = ?",
            (snapshot_id,)).fetchone()
        return ConsensusSnapshot.model_validate_json(row[0]) if row else None

    def consensus_snapshots(self, stock_id: str,
                            fiscal_quarter: Optional[str] = None
                            ) -> list[ConsensusSnapshot]:
        q = "SELECT payload FROM consensus_snapshots WHERE stock_id = ?"
        args: list[Any] = [stock_id]
        if fiscal_quarter is not None:
            q += " AND fiscal_quarter = ?"
            args.append(fiscal_quarter)
        rows = [ConsensusSnapshot.model_validate_json(r[0])
                for r in self._conn.execute(q, args).fetchall()]
        return sorted(rows, key=lambda s: _instant(s.captured_at))

    def consensus_in_force(self, stock_id: str, fiscal_quarter: str,
                           as_of: Optional[datetime] = None
                           ) -> Optional[ConsensusSnapshot]:
        """The newest capture at or before `as_of` — "what was expected just
        before the release", which is the figure the print is judged against.
        """
        rows = self.consensus_snapshots(stock_id, fiscal_quarter)
        if as_of is not None:
            cutoff = _instant(as_of)
            rows = [r for r in rows if _instant(r.captured_at) <= cutoff]
        return rows[-1] if rows else None

    def _newest_consensus(self, stock_id: str, fiscal_quarter: str
                          ) -> Optional[ConsensusSnapshot]:
        rows = self.consensus_snapshots(stock_id, fiscal_quarter)
        return rows[-1] if rows else None

    # -- earnings prints (append-only; deepening appends) ---------------------

    def record_print(self, print_row: EarningsPrint) -> str:
        """Record one reading of a quarter. Returns its id.

        When the caller did not pin a consensus row, the one in force just
        before the release is pinned here — that pointer, not an overwrite,
        is how "this quarter's consensus is now fixed" is expressed.
        """
        if not print_row.consensus_snapshot_id:
            moment = print_row.reported_at or datetime.combine(
                print_row.report_date, time.max, tzinfo=timezone.utc)
            in_force = self.consensus_in_force(
                print_row.stock_id, print_row.fiscal_quarter, as_of=moment)
            if in_force is not None:
                print_row = print_row.model_copy(
                    update={"consensus_snapshot_id": in_force.id})
        try:
            self._conn.execute(
                "INSERT INTO earnings_prints (id, stock_id, fiscal_quarter,"
                " report_date, depth, consensus_snapshot_id, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (print_row.id, print_row.stock_id, print_row.fiscal_quarter,
                 print_row.report_date.isoformat(), print_row.depth.value,
                 print_row.consensus_snapshot_id, print_row.model_dump_json()))
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise ValueError(
                f"{print_row.stock_id} {print_row.fiscal_quarter} is already "
                f"recorded at depth '{print_row.depth.value}'; record a "
                f"DEEPER reading instead of repeating this one ({exc})"
            ) from exc
        return print_row.id

    def print_row(self, print_id: str) -> Optional[EarningsPrint]:
        row = self._conn.execute(
            "SELECT payload FROM earnings_prints WHERE id = ?",
            (print_id,)).fetchone()
        return EarningsPrint.model_validate_json(row[0]) if row else None

    def prints(self, stock_id: str,
               fiscal_quarter: Optional[str] = None) -> list[EarningsPrint]:
        q = "SELECT payload FROM earnings_prints WHERE stock_id = ?"
        args: list[Any] = [stock_id]
        if fiscal_quarter is not None:
            q += " AND fiscal_quarter = ?"
            args.append(fiscal_quarter)
        rows = [EarningsPrint.model_validate_json(r[0])
                for r in self._conn.execute(q, args).fetchall()]
        return sorted(rows, key=lambda p: (p.report_date, p.depth.rank))

    def latest_print(self, stock_id: str,
                     fiscal_quarter: str) -> Optional[EarningsPrint]:
        """The deepest reading of that quarter."""
        rows = self.prints(stock_id, fiscal_quarter)
        return rows[-1] if rows else None

    def deepest_prints(self, stock_id: str) -> list[EarningsPrint]:
        by_quarter: dict[str, EarningsPrint] = {}
        for row in self.prints(stock_id):
            by_quarter[row.fiscal_quarter] = row      # sorted, so deepest wins
        return sorted(by_quarter.values(), key=lambda p: p.report_date)

    # -- the joined single-stock read -----------------------------------------

    def history(self, stock_id: str) -> Optional[StockHistory]:
        stock = self.stock(stock_id)
        if stock is None:
            return None
        prints = self.deepest_prints(stock_id)
        frozen: dict[str, ConsensusSnapshot] = {}
        for row in prints:
            snapshot = (self.consensus(row.consensus_snapshot_id)
                        if row.consensus_snapshot_id else None)
            if snapshot is None:
                snapshot = self.consensus_in_force(stock_id, row.fiscal_quarter)
            if snapshot is not None:
                frozen[row.fiscal_quarter] = snapshot
        return StockHistory(stock=stock, prints=prints, consensus=frozen,
                            decisions=self._decisions(stock.ticker),
                            screened=self._screened(stock.ticker))

    def _has_table(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,)).fetchone() is not None

    def _decisions(self, ticker: str) -> list[dict]:
        """Past tribunal decisions on this ticker, newest last.

        Read by ticker because the existing tables predate the master and are
        deliberately not being rewritten (the user's instruction). New rows
        will carry `stock_id`; old ones can only be matched this way, and a
        reused ticker is the known limitation of doing so.
        """
        if not self._has_table("recommendations"):
            return []
        rows = self._conn.execute(
            "SELECT id, created_at, status,"
            " json_extract(payload, '$.verdict.decision'),"
            " json_extract(payload, '$.brief.catalyst.event_date')"
            " FROM recommendations WHERE ticker = ?", (ticker,)).fetchall()
        out = [{"id": r[0], "created_at": r[1], "status": r[2],
                "decision": r[3], "event_date": r[4]} for r in rows]
        return sorted(out, key=lambda d: _instant(d["created_at"]))

    def _screened(self, ticker: str) -> list[dict]:
        if not self._has_table("screened_candidates"):
            return []
        rows = self._conn.execute(
            "SELECT id, recorded_at, stage, payload FROM screened_candidates"
            " WHERE ticker = ?", (ticker,)).fetchall()
        out = []
        for row in rows:
            payload = json.loads(row[3])
            out.append({"id": row[0], "recorded_at": row[1], "stage": row[2],
                        "event_date": payload.get("event_date"),
                        "eps_surprise_pct": payload.get("eps_surprise_pct"),
                        # Carried so the "is this company worth following"
                        # projection can be rebuilt from the ledger like
                        # every other projection here (§6.1(E)).
                        "gate_report": payload.get("gate_report")})
        return sorted(out, key=lambda d: _instant(d["recorded_at"]))

    # -- projection rebuild ---------------------------------------------------

    def rebuild_projection(self, ledger=None) -> int:
        """Recreate every master's review projection from the ledger.

        Required by design: a projection that cannot be rebuilt quietly
        becomes a second source of truth, and then a disagreement between it
        and the ledger has no resolution rule.

        The decision tables are read through this store's own connection, so
        a ledger opened on a different file is refused rather than producing
        a confident "0 rebuilt" that only means it looked in the wrong place.
        """
        if ledger is not None and getattr(ledger, "path", self.path) != self.path:
            raise ValueError(
                f"rebuild_projection reads the ledger tables in its own "
                f"database ({self.path}); a ledger on {ledger.path} cannot "
                f"be rebuilt from here")
        rebuilt = 0
        for stock in self.stocks():
            history = self.history(stock.id)
            if history is None:
                continue
            latest = self._latest_review(history)
            if latest is None:
                continue
            quarter, at, stage = latest
            self.record_review(stock.id, quarter, stage, reviewed_at=at)
            rebuilt += 1
        return rebuilt

    def _latest_review(self, history: StockHistory
                       ) -> Optional[tuple[str, datetime, ReviewStage]]:
        events: list[tuple[datetime, str, ReviewStage]] = []
        for decision in history.decisions:
            stage = (ReviewStage.BUY if decision.get("decision") == "buy"
                     else ReviewStage.TRIBUNAL_PASS)
            events.append((_instant(decision["created_at"]),
                           self._quarter_for(history, decision.get("event_date")),
                           stage))
        for row in history.screened:
            stage = (ReviewStage.GATE_REJECT
                     if row.get("stage") == "gate_reject" else ReviewStage.SCREENED)
            events.append((_instant(row["recorded_at"]),
                           self._quarter_for(history, row.get("event_date")),
                           stage))
        if not events:
            return None
        at, quarter, stage = max(events, key=lambda e: e[0])
        return quarter, to_jst(at), stage

    def _quarter_for(self, history: StockHistory,
                     event_date: Optional[str]) -> str:
        """The fiscal quarter a decision was about.

        The label on the recorded print, or nothing. There used to be a
        fallback to the calendar quarter of the event date; it is gone
        because this feeds `last_reviewed_fiscal_quarter`, which
        `already_reviewed` compares against a print's real label — and a
        fabricated label there can never match, so the guard it was meant to
        arm silently did nothing (EW移行 §2).
        """
        if not event_date:
            return ""
        try:
            day = date.fromisoformat(str(event_date)[:10])
        except ValueError:
            return ""
        for row in history.prints:
            if abs((row.report_date - day).days) <= 1:
                return row.fiscal_quarter
        return ""
