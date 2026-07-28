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
from datetime import date, timezone
from typing import Any, Optional

from hawkeye.contracts.models import (
    Outcome,
    Recommendation,
    RecommendationStatus,
    utcnow,
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
"""

_GENESIS = "0" * 64


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
    return h.hexdigest()


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
        q += " ORDER BY created_at"
        return [
            {"id": r[0], "created_at": r[1], "ticker": r[2], "status": r[3]}
            for r in self._conn.execute(q, args).fetchall()
        ]

    def open_positions(self) -> list[Recommendation]:
        rows = self._conn.execute(
            "SELECT payload FROM recommendations WHERE status = ? ORDER BY created_at",
            (RecommendationStatus.OPEN.value,)).fetchall()
        return [Recommendation.model_validate_json(r[0]) for r in rows]

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
            ts = utcnow().isoformat()
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
        prev_hash = _GENESIS
        for seq, rec_id, ts, kind, payload, stored_prev, stored_hash in self._conn.execute(
                "SELECT seq, rec_id, ts, kind, payload, prev_hash, hash"
                " FROM journal ORDER BY seq"):
            if stored_prev != prev_hash:
                return False
            if _sha(stored_prev, ts, rec_id, kind, payload) != stored_hash:
                return False
            prev_hash = stored_hash
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
                    tickers: list[str]) -> None:
        self._conn.execute(
            "INSERT INTO scans (ts, params, scanned, screened, enriched,"
            " gate_passed, tickers) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (utcnow().isoformat(), json.dumps(params, default=str),
             scanned, screened, enriched, gate_passed, json.dumps(tickers)))
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
