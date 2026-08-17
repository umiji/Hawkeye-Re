"""Staging the cause extraction for session mode (T-003).

The sibling of `hawkeye/scout/guidance_case.py`, and it exists for the same
reason that one does: the tribunal is driven by a Claude Code session, and a
Python subprocess in the middle of a scan has no way to ask it for anything.
The call has to go out through the CLI and come back through the CLI. So the
scan writes down the summary it would have asked about, and two commands
close the loop afterwards:

    hawkeye scout                         stages one file per print
    hawkeye cause queue                   emits the package for one of them
    hawkeye cause submit <id> --file r    validates it and attaches it

The consequence is the same one the guidance queue carries: the print row is
written BEFORE the reading is known, so attaching it retires that row and
appends a corrected one (`StockStore.revise_print`). The ledger then records
what actually happened rather than pretending the row was complete all along.

**Nothing here changes a number.** The reading is a note about WHY the
reported figures came out where they did; the figures themselves are what
the scan stood behind and are not this step's to revise (invariant 1,
invariant 6). Attaching one leaves the score exactly where it was — which is
also why, unlike guidance, no re-ranking depends on this queue being empty.

Nothing in this file decides anything about the reading, and it deliberately
does not wrap the thing that does: callers reach for `cause_agent`'s own
`render_request` and `parse_reply`, so there is no second place where the two
modes could drift into accepting different answers.
"""
from __future__ import annotations

import json
import sys
from typing import Optional

from pydantic import BaseModel, Field

from hawkeye import paths
from hawkeye.contracts.models import new_id, now
from hawkeye.scout.cause_agent import CauseExtraction, CauseRequest
from hawkeye.scout.revision import target_row


class CauseCase(BaseModel):
    """One print's summary, waiting to be read for what it explains.

    `print_id` is what the reply is attached to. Carrying the id rather than
    re-deriving the row from the ticker and quarter matters: between the scan
    and the submission the vendor can restate a figure, and that appends a new
    row — attaching to whatever is active at submit time would silently move
    the reading onto a row nobody read the summary for.
    """
    id: str = Field(default_factory=lambda: new_id("cau"))
    stock_id: str
    print_id: str
    ticker: str
    fiscal_quarter: str
    summary: str

    def request(self) -> CauseRequest:
        """What the agent is shown. The quarter just reported and the summary,
        and nothing about how far the print cleared consensus — see the
        `cause_agent` module docstring for why that omission is the point."""
        return CauseRequest(ticker=self.ticker,
                            fiscal_quarter=self.fiscal_quarter,
                            summary=self.summary)


# --- the file queue ---------------------------------------------------------

def _case_path(case_id: str):
    return paths.cause_dir() / f"{case_id}.json"


def save_case(case: CauseCase) -> None:
    paths.cause_dir().mkdir(parents=True, exist_ok=True)
    _case_path(case.id).write_text(case.model_dump_json(indent=2),
                                   encoding="utf-8")


def load_case(case_id: str) -> CauseCase:
    return CauseCase.model_validate_json(
        _case_path(case_id).read_text(encoding="utf-8"))


def list_cases() -> list[CauseCase]:
    """Everything still waiting, by ticker.

    An unreadable file is reported rather than skipped: a queue that quietly
    shrinks looks exactly like a queue that was worked through.
    """
    d = paths.cause_dir()
    if not d.exists():
        return []
    cases: list[CauseCase] = []
    for p in sorted(d.glob("cau_*.json")):
        try:
            cases.append(CauseCase.model_validate_json(
                p.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"warning: unreadable cause case {p}: {exc}", file=sys.stderr)
    return sorted(cases, key=lambda c: c.ticker)


def discard(case_id: str) -> bool:
    """Delete a staged case. Only after its ledger write is confirmed — the
    staged file is what makes a failed write retryable."""
    p = _case_path(case_id)
    if not p.exists():
        return False
    p.unlink()
    return True


# --- submitting -------------------------------------------------------------

def load_reply(path: str) -> dict:
    raw = json.loads(open(path, encoding="utf-8").read())
    if not isinstance(raw, dict):
        raise ValueError("cause reply must be a JSON object")
    return raw


def attach(store, case: CauseCase,
           extraction: CauseExtraction) -> Optional[str]:
    """Put the reading (or the named refusal) on the print row.

    Returns the new row's id, or None when the row the case was staged for is
    no longer the active one — which means a restatement landed in between,
    and the summary this reading came from described the retired row. Refusing
    there is the same rule as everywhere else: a reading whose subject moved
    is not a reading (invariant 6).
    """
    active = target_row(store, case.stock_id, case.fiscal_quarter,
                        case.print_id)
    if active is None:
        return None
    # A NEW id, because this is a new row rather than an edit of the old one.
    # Reusing the id would collide with the row being retired in the same
    # transaction, and the append-only table would refuse it.
    return store.revise_print(active.model_copy(update={
        "id": new_id("ern"),
        "recorded_at": now(),
        "cause": extraction.reading,
        "cause_reason": extraction.reason}))
