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
from datetime import date, datetime, timezone
from typing import Any, Optional

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
        # A recorded purge restates what a scan's rows should now hash to, so
        # the anchor event for that scan is checked against the LATEST such
        # restatement rather than against the batch as first written. Read
        # ahead in one pass: the purge event is chained like any other, so it
        # cannot be forged, and a deletion with no purge event still fails.
        purged: dict[Any, dict] = {}
        for (payload,) in self._conn.execute(
                "SELECT payload FROM journal WHERE kind ="
                " 'screened_candidates_purged' ORDER BY seq"):
            body = json.loads(payload)
            purged[body.get("scan_id")] = body

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
                body = purged.get(body.get("scan_id"), body)
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

        `actual_pending` is the one drop stage that does NOT count as seen.
        Those rows say the print's own numbers had not arrived and it was
        never judged, so counting them would close the door the hold exists
        to keep open — the print would be recorded as pending once and never
        read again (hawkeye/scout/waiting.py). Every other stage, including
        `actual_timeout`, is a decision that was actually taken.
        """
        out: set[tuple[str, date]] = set()
        queries = (
            "SELECT json_extract(payload, '$.ticker'),"
            " json_extract(payload, '$.event_date') FROM screened_candidates"
            " WHERE stage != 'actual_pending'",
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

    def purge_screened_candidates(self, ids: list[str]) -> int:
        """Delete held rows by id, recording the deletion in the journal.

        `record_screened_candidates` anchors each scan's rows as one journal
        event carrying a count and a hash of the payloads, so a row removed
        behind the chain's back makes `verify_chain()` report tampering
        forever. Journalling the removal is what keeps that check meaningful:
        afterwards the chain no longer asserts "nothing was ever deleted" — it
        asserts the stronger and still-true "nothing was deleted without this
        ledger saying so, by whom, and which rows".

        Refuses any row a drop-candidate review points at. The planner in
        `hawkeye/ledger/purge.py` reports the same rule as advice; this is the
        wall behind it, because a caller that skipped the planner would
        otherwise leave a review pointing at nothing.
        """
        if not ids:
            return 0
        wanted = list(dict.fromkeys(ids))
        placeholders = ",".join("?" * len(wanted))
        referenced = [r[0] for r in self._conn.execute(
            "SELECT DISTINCT screened_candidate_id FROM drop_reviews"
            f" WHERE screened_candidate_id IN ({placeholders})",
            wanted).fetchall()]
        if referenced:
            raise ValueError(
                f"{len(referenced)} of these rows are cited by a drop review "
                f"({', '.join(sorted(referenced)[:5])}) — deleting them would "
                f"leave the review's verdict pointing at nothing")

        rows = self._conn.execute(
            f"SELECT id, scan_id FROM screened_candidates"
            f" WHERE id IN ({placeholders})", wanted).fetchall()
        by_scan: dict[int, list[str]] = {}
        for row_id, scan_id in rows:
            by_scan.setdefault(scan_id, []).append(row_id)
        if not by_scan:
            return 0

        removed = 0
        for scan_id, scan_ids in sorted(by_scan.items()):
            marks = ",".join("?" * len(scan_ids))
            self._conn.execute(
                f"DELETE FROM screened_candidates WHERE id IN ({marks})",
                scan_ids)
            removed += len(scan_ids)
            remaining = [r[0] for r in self._conn.execute(
                "SELECT payload FROM screened_candidates WHERE scan_id = ?",
                (scan_id,)).fetchall()]
            self._conn.commit()
            # The event carries the state the scan's rows are in AFTER the
            # deletion, because that is what a later verification can actually
            # recompute — the removed payloads are gone by then.
            self.append_event(
                str(scan_id), "screened_candidates_purged",
                {"scan_id": scan_id, "removed_ids": sorted(scan_ids),
                 "count": len(remaining),
                 "batch_hash": _sha("\n".join(sorted(remaining)))})
        return removed

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
