"""Append-only decision ledger (SQLite).

Integrity model:
- A recommendation's payload is written once at creation and NEVER mutated.
  Everything that happens afterwards (user decision, trades, signals, claim
  resolutions, outcome) is an append-only journal event referencing it.
- Journal events are hash-chained (each event hashes the previous event's
  hash), making after-the-fact edits detectable. This is the mechanical
  guarantee behind "no position talk": you cannot quietly rewrite what you
  predicted once reality starts disagreeing with you.
- The ``status`` column on recommendations is a projection for convenient
  querying; the journal is the source of truth.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from hawkeye.contracts.stocks import ActualWait, within_wait_window
from hawkeye.contracts.models import (
    DropReview,
    Outcome,
    Recommendation,
    RecommendationStatus,
    ScreenedCandidate,
    new_id,
    now,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recommendations (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    status      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    rec_id     TEXT NOT NULL,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_rec ON journal (rec_id);
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    params      TEXT NOT NULL,
    scanned     INTEGER NOT NULL,
    screened    INTEGER NOT NULL,
    enriched    INTEGER NOT NULL,
    gate_passed INTEGER NOT NULL,
    tickers     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS screened_candidates (
    id          TEXT PRIMARY KEY,
    scan_id     INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    stage       TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screened_scan ON screened_candidates (scan_id);
CREATE INDEX IF NOT EXISTS idx_screened_ticker ON screened_candidates (ticker);
CREATE TABLE IF NOT EXISTS drop_reviews (
    id                    TEXT PRIMARY KEY,
    batch_id              TEXT NOT NULL,
    reviewed_at           TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    cohort                TEXT NOT NULL,
    checkpoint            TEXT NOT NULL,
    screened_candidate_id TEXT NOT NULL DEFAULT '',
    rec_id                TEXT NOT NULL DEFAULT '',
    miss_category         TEXT NOT NULL DEFAULT '',
    payload               TEXT NOT NULL
);
-- The checkpoints are fixed and never re-checked (§5.2(3)). A second row for
-- the same candidate at the same checkpoint would be a re-measurement, i.e.
-- exactly the "run it again until the number flatters" loop the fixed
-- horizons exist to prevent — so the schema refuses one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_drop_review_subject
    ON drop_reviews (screened_candidate_id, rec_id, checkpoint);
CREATE INDEX IF NOT EXISTS idx_drop_review_batch ON drop_reviews (batch_id);
CREATE INDEX IF NOT EXISTS idx_drop_review_ticker ON drop_reviews (ticker);
-- Prints the funnel asked for a document about and has not received one for
-- (hawkeye/scout/release.py). Its own record rather than an exception buried
-- in the dedup: "we are still waiting on this one" is a state somebody has to
-- be able to read, and the wait has to be bounded from the FIRST ask, which
-- means the moment of that ask must be stored.
CREATE TABLE IF NOT EXISTS release_requests (
    id           TEXT PRIMARY KEY,     -- TICKER_YYYY-MM-DD, one per print
    ticker       TEXT NOT NULL,
    report_date  TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    resolved_at  TEXT NOT NULL DEFAULT '',
    resolution   TEXT NOT NULL DEFAULT ''
);
-- Prints whose numbers have not arrived yet (hawkeye/scout/waiting.py). Its
-- own table for the same reason release_requests has one: "we are waiting on
-- this, and here is every time we looked and what we got" is a state, and the
-- two tables it could otherwise hide in are append-only by design.
--
-- `announced_at` is written once and never moved: it is the origin of the
-- 48-hour clock, and a later sighting overwriting it would extend the wait
-- indefinitely, one scan at a time. `checks` accumulates one entry per read
-- so that "the feed never had it" and "the feed kept answering with the
-- previous quarter" stay distinguishable after the fact.
CREATE TABLE IF NOT EXISTS earnings_actual_waits (
    id              TEXT PRIMARY KEY,   -- TICKER_YYYY-MM-DD, one per print
    ticker          TEXT NOT NULL,
    report_date     TEXT NOT NULL,
    announced_at    TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    checks          TEXT NOT NULL DEFAULT '[]',
    resolved_at     TEXT NOT NULL DEFAULT '',
    resolution      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_actual_waits_open
    ON earnings_actual_waits (resolved_at);
"""

_GENESIS = "0" * 64


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
    return h.hexdigest()


def _instant(value: datetime | str) -> datetime:
    """A stored timestamp as a comparable instant, for ordering.

    Rows written before 2026-07-31 carry `+00:00` and everything after
    carries `+09:00` (JST). SQLite's ``ORDER BY`` compares those as raw text,
    which puts them in the wrong order wherever the two meet — "2026-08-01
    00:30+09:00" is an EARLIER moment than "2026-07-31 23:00+00:00" but sorts
    after it. Chronological ordering therefore happens here, on real
    instants, not in SQL.

    A value that can't be parsed at all (only reachable by hand-editing the
    database) sorts first rather than taking down the whole listing; the row
    itself is still returned, so nothing disappears silently.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Ledger:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path)
        # Wait for other processes' write locks instead of failing instantly
        # (append_event below holds one across a read+insert).
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- recommendations ----------------------------------------------------

    def record_recommendation(self, rec: Recommendation,
                              status: RecommendationStatus) -> None:
        payload = rec.model_dump_json()
        self._conn.execute(
            "INSERT INTO recommendations (id, created_at, ticker, status, payload, hash)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (rec.id, rec.created_at.isoformat(), rec.ticker, status.value,
             payload, _sha(payload)),
        )
        self._conn.commit()
        self.append_event(rec.id, "recommendation_recorded",
                          {"status": status.value, "payload_hash": _sha(payload)})

    def get(self, rec_id: str) -> Optional[Recommendation]:
        row = self._conn.execute(
            "SELECT payload FROM recommendations WHERE id = ?", (rec_id,)).fetchone()
        return Recommendation.model_validate_json(row[0]) if row else None

    def status(self, rec_id: str) -> Optional[RecommendationStatus]:
        row = self._conn.execute(
            "SELECT status FROM recommendations WHERE id = ?", (rec_id,)).fetchone()
        return RecommendationStatus(row[0]) if row else None

    def list(self, status: Optional[RecommendationStatus] = None) -> list[dict]:
        q = "SELECT id, created_at, ticker, status FROM recommendations"
        args: tuple = ()
        if status is not None:
            q += " WHERE status = ?"
            args = (status.value,)
        rows = [
            {"id": r[0], "created_at": r[1], "ticker": r[2], "status": r[3]}
            for r in self._conn.execute(q, args).fetchall()
        ]
        return sorted(rows, key=lambda r: _instant(r["created_at"]))

    def open_positions(self) -> list[Recommendation]:
        rows = self._conn.execute(
            "SELECT payload FROM recommendations WHERE status = ?",
            (RecommendationStatus.OPEN.value,)).fetchall()
        return sorted((Recommendation.model_validate_json(r[0]) for r in rows),
                      key=lambda rec: _instant(rec.created_at))

    def _set_status(self, rec_id: str, status: RecommendationStatus) -> None:
        self._conn.execute("UPDATE recommendations SET status = ? WHERE id = ?",
                           (status.value, rec_id))
        self._conn.commit()

    # -- journal (append-only, hash-chained) --------------------------------

    def append_event(self, rec_id: str, kind: str, payload: dict[str, Any]) -> str:
        # BEGIN IMMEDIATE takes the write lock before the read, so the
        # read-prev-hash + insert-next-row pair is atomic against other
        # writers. Without this, two processes could both read the same
        # prev_hash and each insert a row claiming it, corrupting the chain
        # (a break verify_chain() can only detect after the fact, not undo).
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            prev = self._conn.execute(
                "SELECT hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = prev[0] if prev else _GENESIS
            ts = now().isoformat()
            body = json.dumps(payload, sort_keys=True, default=str)
            h = _sha(prev_hash, ts, rec_id, kind, body)
            self._conn.execute(
                "INSERT INTO journal (rec_id, ts, kind, payload, prev_hash, hash)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (rec_id, ts, kind, body, prev_hash, h))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return h

    def events(self, rec_id: str, kind: Optional[str] = None) -> list[dict]:
        q = "SELECT seq, ts, kind, payload FROM journal WHERE rec_id = ?"
        args: list = [rec_id]
        if kind is not None:
            q += " AND kind = ?"
            args.append(kind)
        q += " ORDER BY seq"
        return [
            {"seq": r[0], "ts": r[1], "kind": r[2], "payload": json.loads(r[3])}
            for r in self._conn.execute(q, args).fetchall()
        ]

    def verify_chain(self) -> bool:
        """Verify the journal's hash chain AND that every recorded
        recommendation's current payload still matches the payload hash
        captured in its (tamper-evident) `recommendation_recorded` journal
        event. Without the second check, rewriting `recommendations.payload`
        directly via SQL — and updating its own `hash` column to match —
        would pass unnoticed, since that column isn't chained to anything;
        only the journal event's `payload_hash` is protected by the chain.
        """
        prev_hash = _GENESIS
        for seq, rec_id, ts, kind, payload, stored_prev, stored_hash in self._conn.execute(
                "SELECT seq, rec_id, ts, kind, payload, prev_hash, hash"
                " FROM journal ORDER BY seq"):
            if stored_prev != prev_hash:
                return False
            if _sha(stored_prev, ts, rec_id, kind, payload) != stored_hash:
                return False
            prev_hash = stored_hash
            if kind == "recommendation_recorded":
                expected_hash = json.loads(payload).get("payload_hash")
                row = self._conn.execute(
                    "SELECT payload FROM recommendations WHERE id = ?",
                    (rec_id,)).fetchone()
                if row is None or _sha(row[0]) != expected_hash:
                    return False
            if kind == "screened_candidates_recorded":
                body = json.loads(payload)
                rows = self._conn.execute(
                    "SELECT payload FROM screened_candidates WHERE scan_id = ?",
                    (body.get("scan_id"),)).fetchall()
                if len(rows) != body.get("count"):
                    return False
                current_hash = _sha("\n".join(sorted(r[0] for r in rows)))
                if current_hash != body.get("batch_hash"):
                    return False
            if kind == "drop_reviews_recorded":
                body = json.loads(payload)
                rows = self._conn.execute(
                    "SELECT payload FROM drop_reviews WHERE batch_id = ?",
                    (body.get("batch_id"),)).fetchall()
                if len(rows) != body.get("count"):
                    return False
                current_hash = _sha("\n".join(sorted(r[0] for r in rows)))
                if current_hash != body.get("batch_hash"):
                    return False
        return True

    # -- lifecycle events ----------------------------------------------------

    def record_decision(self, rec_id: str, approved: bool, note: str = "") -> None:
        self.append_event(rec_id, "user_decision",
                          {"approved": approved, "note": note})
        self._set_status(rec_id, RecommendationStatus.APPROVED if approved
                         else RecommendationStatus.DECLINED)

    def record_entry(self, rec_id: str, price: float, shares: int,
                     trade_date: date) -> None:
        self.append_event(rec_id, "entry_trade",
                          {"price": price, "shares": shares,
                           "date": trade_date.isoformat()})
        self._set_status(rec_id, RecommendationStatus.OPEN)

    def record_exit(self, rec_id: str, price: float, trade_date: date,
                    note: str = "") -> None:
        self.append_event(rec_id, "exit_trade",
                          {"price": price, "date": trade_date.isoformat(),
                           "note": note})
        self._set_status(rec_id, RecommendationStatus.CLOSED)

    def record_signal(self, rec_id: str, signal: dict[str, Any]) -> None:
        self.append_event(rec_id, "sentinel_signal", signal)

    def resolve_claim(self, rec_id: str, claim_id: str, outcome: bool,
                      note: str = "") -> None:
        self.append_event(rec_id, "claim_resolution",
                          {"claim_id": claim_id, "outcome": outcome, "note": note})

    def claim_resolutions(self, rec_id: str) -> dict[str, tuple[bool, str]]:
        """claim_id -> (outcome, note); the latest resolution wins."""
        out: dict[str, tuple[bool, str]] = {}
        for ev in self.events(rec_id, kind="claim_resolution"):
            p = ev["payload"]
            out[p["claim_id"]] = (bool(p["outcome"]), p.get("note", ""))
        return out

    def entry(self, rec_id: str) -> Optional[dict]:
        evs = self.events(rec_id, kind="entry_trade")
        return evs[0]["payload"] if evs else None

    def exit(self, rec_id: str) -> Optional[dict]:
        evs = self.events(rec_id, kind="exit_trade")
        return evs[-1]["payload"] if evs else None

    def record_outcome(self, outcome: Outcome) -> None:
        self.append_event(outcome.recommendation_id, "outcome",
                          json.loads(outcome.model_dump_json()))

    def outcome(self, rec_id: str) -> Optional[Outcome]:
        evs = self.events(rec_id, kind="outcome")
        return Outcome.model_validate(evs[-1]["payload"]) if evs else None

    # -- scout scans (funnel audit trail) ------------------------------------

    def record_scan(self, params: dict, scanned: int, screened: int,
                    enriched: int, gate_passed: int,
                    tickers: list[str]) -> int:
        """Returns the new scan's id, so callers can tag
        record_screened_candidates() rows to the scan that dropped them."""
        cur = self._conn.execute(
            "INSERT INTO scans (ts, params, scanned, screened, enriched,"
            " gate_passed, tickers) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now().isoformat(), json.dumps(params, default=str),
             scanned, screened, enriched, gate_passed, json.dumps(tickers)))
        self._conn.commit()
        return cur.lastrowid

    def last_scan_at(self) -> Optional[datetime]:
        """When the most recent scan ran — the anchor for the next scan's
        lookback window (docs/design/MASTER_OVERVIEW.ja.md §5.2(1))."""
        row = self._conn.execute(
            "SELECT ts FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except ValueError:
            return None

    def seen_events(self) -> set[tuple[str, date]]:
        """Every (ticker, catalyst date) this system has already acted on.

        Both tables are needed: `screened_candidates` holds only what was
        DROPPED, so a candidate that went to the tribunal appears solely in
        `recommendations` and would otherwise come back on the next
        overlapping window.

        Read as raw JSON rather than through the models on purpose — dedup
        going blind because one old row predates a model change would
        silently re-evaluate candidates, which is worse than the parse
        error it would otherwise raise.
        """
        out: set[tuple[str, date]] = set()
        queries = (
            "SELECT json_extract(payload, '$.ticker'),"
            " json_extract(payload, '$.event_date') FROM screened_candidates",
            "SELECT json_extract(payload, '$.ticker'),"
            " json_extract(payload, '$.brief.catalyst.event_date')"
            " FROM recommendations",
        )
        for query in queries:
            for ticker, day in self._conn.execute(query).fetchall():
                if not ticker or not day:
                    continue
                try:
                    out.add((ticker, date.fromisoformat(str(day)[:10])))
                except ValueError:
                    continue
        return out

    # -- prints held open pending a release read -----------------------------

    def request_release_reads(self,
                              prints: list[tuple[str, date]]) -> int:
        """Hold these prints open until their document arrives. Returns how
        many were newly opened.

        Asking again for a print already open changes nothing on purpose: the
        funnel names it on every run until it is settled, and letting the
        latest ask win would keep restarting the clock — a bounded wait would
        quietly become an unbounded one.
        """
        opened = 0
        for ticker, day in prints:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO release_requests"
                " (id, ticker, report_date, requested_at) VALUES (?, ?, ?, ?)",
                (f"{ticker.upper()}_{day.isoformat()}", ticker.upper(),
                 day.isoformat(), now().isoformat()))
            opened += cur.rowcount
        self._conn.commit()
        return opened

    def open_release_requests(self, today: date,
                              max_age_days: int) -> set[tuple[str, date]]:
        """Prints still worth going back for, as (ticker, report date).

        Bounded by age because a document that never arrives would otherwise
        cost a calendar lookup and an enrichment on every run forever. The
        bound is the one the entry gates already impose: past
        `max_event_age_days` trading days the catalyst is too old to trade,
        so a leg settled after that could not produce a position anyway.
        """
        cutoff = today - timedelta(days=max_age_days)
        out: set[tuple[str, date]] = set()
        for ticker, day in self._conn.execute(
                "SELECT ticker, report_date FROM release_requests"
                " WHERE resolved_at = ''").fetchall():
            try:
                report_date = date.fromisoformat(day)
            except ValueError:
                continue
            if report_date >= cutoff:
                out.add((ticker, report_date))
        return out

    def resolve_release_reads(self, prints: list[tuple[str, date]],
                              resolution: str) -> None:
        """Close the wait. `resolution` says why — a document was read, or
        the wait ran out — because "we read it and it settled nothing" and
        "nobody ever produced it" are different facts about the same print.
        """
        for ticker, day in prints:
            self._conn.execute(
                "UPDATE release_requests SET resolved_at = ?, resolution = ?"
                " WHERE id = ? AND resolved_at = ''",
                (now().isoformat(), resolution,
                 f"{ticker.upper()}_{day.isoformat()}"))
        self._conn.commit()

    def expire_release_requests(self, today: date, max_age_days: int) -> int:
        """Mark the waits that ran out, so an open request means a print
        somebody can still do something about. Returns how many expired."""
        cutoff = (today - timedelta(days=max_age_days)).isoformat()
        cur = self._conn.execute(
            "UPDATE release_requests SET resolved_at = ?, resolution = 'expired'"
            " WHERE resolved_at = '' AND report_date < ?",
            (now().isoformat(), cutoff))
        self._conn.commit()
        return cur.rowcount

    # -- prints held open pending their own numbers ---------------------------

    @staticmethod
    def _wait_from_row(row) -> ActualWait:
        return ActualWait(
            ticker=row[0], report_date=date.fromisoformat(row[1]),
            announced_at=_instant(row[2]), first_seen_at=_instant(row[3]),
            last_checked_at=_instant(row[4]), attempts=row[5],
            checks=json.loads(row[6]),
            resolved_at=_instant(row[7]) if row[7] else None,
            resolution=row[8])

    def note_missing_actual(self, ticker: str, report_date: date,
                            announced_at: datetime, reason: str,
                            at: Optional[datetime] = None) -> ActualWait:
        """Record that this print's numbers were looked for and not found.

        Opens the wait the first time and appends a check every time. The
        announcement time and the first sighting are written once: they are
        what bounds the wait, and letting either move would turn a bounded
        wait into a permanent one (the same trap release_requests documents).
        """
        moment = at or now()
        key = f"{ticker.upper()}_{report_date.isoformat()}"
        existing = self.actual_wait(ticker, report_date)
        if existing is not None and existing.resolved_at:
            return existing            # closed waits stay closed
        checks = (existing.checks if existing else []) + [
            {"at": moment.isoformat(), "reason": reason}]
        if existing is None:
            self._conn.execute(
                "INSERT INTO earnings_actual_waits (id, ticker, report_date,"
                " announced_at, first_seen_at, last_checked_at, attempts,"
                " checks) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, ticker.upper(), report_date.isoformat(),
                 announced_at.isoformat(), moment.isoformat(),
                 moment.isoformat(), 1, json.dumps(checks)))
        else:
            self._conn.execute(
                "UPDATE earnings_actual_waits SET last_checked_at = ?,"
                " attempts = attempts + 1, checks = ? WHERE id = ?",
                (moment.isoformat(), json.dumps(checks), key))
        self._conn.commit()
        return self.actual_wait(ticker, report_date)

    def actual_wait(self, ticker: str,
                    report_date: date) -> Optional[ActualWait]:
        row = self._conn.execute(
            "SELECT ticker, report_date, announced_at, first_seen_at,"
            " last_checked_at, attempts, checks, resolved_at, resolution"
            " FROM earnings_actual_waits WHERE id = ?",
            (f"{ticker.upper()}_{report_date.isoformat()}",)).fetchone()
        return self._wait_from_row(row) if row else None

    def _open_waits(self) -> list[ActualWait]:
        return [self._wait_from_row(r) for r in self._conn.execute(
            "SELECT ticker, report_date, announced_at, first_seen_at,"
            " last_checked_at, attempts, checks, resolved_at, resolution"
            " FROM earnings_actual_waits WHERE resolved_at = ''"
            " ORDER BY report_date, ticker").fetchall()]

    def open_actual_waits(self, now_at: datetime,
                          hours: int) -> list[ActualWait]:
        """Prints still inside the window, for the next scan to re-read."""
        return [w for w in self._open_waits()
                if within_wait_window(w.announced_at, now_at, hours)]

    def expire_actual_waits(self, now_at: datetime,
                            hours: int) -> list[ActualWait]:
        """Close the waits whose 48 hours ran out; returns exactly the ones
        closed by THIS call, so a caller can report them without counting a
        print twice across runs."""
        expired = [w for w in self._open_waits()
                   if not within_wait_window(w.announced_at, now_at, hours)]
        for wait in expired:
            self._close(wait.ticker, wait.report_date, "expired_48h", now_at)
        return [self.actual_wait(w.ticker, w.report_date) for w in expired]

    def resolve_actual_wait(self, ticker: str, report_date: date,
                            resolution: str,
                            at: Optional[datetime] = None) -> None:
        """End the wait because the numbers turned up."""
        self._close(ticker, report_date, resolution, at or now())

    def _close(self, ticker: str, report_date: date, resolution: str,
               at: datetime) -> None:
        self._conn.execute(
            "UPDATE earnings_actual_waits SET resolved_at = ?, resolution = ?"
            " WHERE id = ? AND resolved_at = ''",
            (at.isoformat(), resolution,
             f"{ticker.upper()}_{report_date.isoformat()}"))
        self._conn.commit()

    def list_scans(self) -> list[dict]:
        return [
            {"ts": r[0], "params": json.loads(r[1]), "scanned": r[2],
             "screened": r[3], "enriched": r[4], "gate_passed": r[5],
             "tickers": json.loads(r[6])}
            for r in self._conn.execute(
                "SELECT ts, params, scanned, screened, enriched, gate_passed,"
                " tickers FROM scans ORDER BY id").fetchall()
        ]

    # -- screened-but-dropped candidates (docs/design/MASTER_OVERVIEW.ja.md §5.1) ---

    def record_screened_candidates(self, scan_id: int,
                                   candidates: list[ScreenedCandidate]) -> None:
        """Persist every candidate a scan dropped, then anchor the whole
        batch's integrity as ONE tamper-evident journal event. Hash-chaining
        every individual row wasn't worth the per-row write overhead (this
        table can hold hundreds of rows per scan) — but the aggregate batch
        hash is exactly the kind of evidence the project's own Phase-0
        continuation decision depends on, so it must not be quietly editable
        after the fact either.
        """
        if not candidates:
            return
        for c in candidates:
            payload = c.model_dump_json()
            self._conn.execute(
                "INSERT INTO screened_candidates"
                " (id, scan_id, recorded_at, ticker, stage, payload)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (c.id, scan_id, c.recorded_at.isoformat(), c.ticker,
                 c.stage.value, payload))
        self._conn.commit()
        batch_repr = "\n".join(sorted(c.model_dump_json() for c in candidates))
        self.append_event(str(scan_id), "screened_candidates_recorded",
                          {"scan_id": scan_id, "count": len(candidates),
                           "batch_hash": _sha(batch_repr)})

    def screened_candidates(self, scan_id: Optional[int] = None,
                            stage: Optional[str] = None
                            ) -> list[ScreenedCandidate]:
        q = "SELECT payload FROM screened_candidates"
        conds: list[str] = []
        args: list[Any] = []
        if scan_id is not None:
            conds.append("scan_id = ?")
            args.append(scan_id)
        if stage is not None:
            conds.append("stage = ?")
            args.append(stage)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        return sorted((ScreenedCandidate.model_validate_json(r[0])
                       for r in self._conn.execute(q, args).fetchall()),
                      key=lambda c: _instant(c.recorded_at))

    # -- drop-candidate reviews (docs/design/MASTER_OVERVIEW.ja.md §5.2(3)) ---------

    def record_drop_reviews(self, reviews: list[DropReview]) -> str:
        """Persist a batch of scored/investigated drops; returns the batch id.

        Separate table, not extra columns on `screened_candidates`: that row
        is what was true at drop time, this is what happened afterwards.

        All-or-nothing. A partially written batch would leave a journal-
        anchored hash that can never match its rows again, i.e. a chain that
        reports tampering forever. A duplicate (candidate, checkpoint) raises
        rather than being skipped — a silently dropped review looks identical
        to a review that was never requested.
        """
        if not reviews:
            return ""
        batch_id = new_id("drb")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for r in reviews:
                self._conn.execute(
                    "INSERT INTO drop_reviews (id, batch_id, reviewed_at,"
                    " ticker, cohort, checkpoint, screened_candidate_id,"
                    " rec_id, miss_category, payload)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (r.id, batch_id, r.reviewed_at.isoformat(), r.ticker,
                     r.cohort, r.checkpoint, r.screened_candidate_id or "",
                     r.rec_id or "",
                     r.miss_category.value if r.miss_category else "",
                     r.model_dump_json()))
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise ValueError(
                f"drop review already recorded for this candidate and "
                f"checkpoint (batch rejected, nothing written): {exc}") from exc
        except Exception:
            self._conn.rollback()
            raise
        batch_repr = "\n".join(sorted(r.model_dump_json() for r in reviews))
        self.append_event(batch_id, "drop_reviews_recorded",
                          {"batch_id": batch_id, "count": len(reviews),
                           "batch_hash": _sha(batch_repr)})
        return batch_id

    def drop_reviews(self, checkpoint: Optional[str] = None,
                     ticker: Optional[str] = None,
                     cohort: Optional[str] = None,
                     miss_category: Optional[str] = None) -> list[DropReview]:
        q = "SELECT payload FROM drop_reviews"
        conds: list[str] = []
        args: list[Any] = []
        for column, value in (("checkpoint", checkpoint), ("ticker", ticker),
                              ("cohort", cohort),
                              ("miss_category", miss_category)):
            if value is not None:
                conds.append(f"{column} = ?")
                args.append(value)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        return sorted((DropReview.model_validate_json(r[0])
                       for r in self._conn.execute(q, args).fetchall()),
                      key=lambda r: _instant(r.reviewed_at))

    def recorded_drop_review_keys(
            self, checkpoint: Optional[str] = None
    ) -> set[tuple[Optional[str], Optional[str], str]]:
        """Subjects already scored, as (screened_candidate_id, rec_id,
        checkpoint) — the same triple the unique index is built on.

        A review round asks for this first so it never spends a price fetch
        re-measuring a subject it cannot store anyway, and so "re-run it and
        see" produces nothing to re-run rather than an insert error halfway
        through a batch.
        """
        q = ("SELECT screened_candidate_id, rec_id, checkpoint "
             "FROM drop_reviews")
        args: list[Any] = []
        if checkpoint is not None:
            q += " WHERE checkpoint = ?"
            args.append(checkpoint)
        return {(r[0], r[1], r[2])
                for r in self._conn.execute(q, args).fetchall()}

    # -- cross-recommendation analytics --------------------------------------

    def all_resolved_claims(self) -> list[tuple[float, bool]]:
        """(stated probability, outcome) for every resolved claim in the book."""
        pairs: list[tuple[float, bool]] = []
        for row in self.list():
            rec = self.get(row["id"])
            if rec is None or rec.thesis is None:
                continue
            resolutions = self.claim_resolutions(rec.id)
            for claim in rec.thesis.claims:
                if claim.id in resolutions:
                    pairs.append((claim.probability, resolutions[claim.id][0]))
        return pairs

    def all_outcomes(self) -> list[Outcome]:
        out = []
        for row in self.list(RecommendationStatus.CLOSED):
            o = self.outcome(row["id"])
            if o is not None:
                out.append(o)
        return out
