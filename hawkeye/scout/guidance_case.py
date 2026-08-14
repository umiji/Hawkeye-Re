"""Staging the guidance extraction for session mode (task 8.7 layer 2).

With an API key the extraction happens inside the scan: `run_scout` is handed
a reader, calls it once per print, and writes the print row already carrying
the reading. Nothing here runs at all.

Session mode cannot do that. The tribunal is driven by a Claude Code session
which spawns one subagent per role, and a Python subprocess in the middle of a
scan has no way to ask it for anything — the call has to go out through the
CLI and come back through the CLI. So the scan writes down the sentence it
would have asked about, and two commands close the loop afterwards:

    hawkeye scout                            stages one file per print
    hawkeye guidance queue                   emits the package for one of them
    hawkeye guidance submit <id> --file r    validates it and attaches it

The consequence, and it is a real cost: in session mode the print row is
written BEFORE the guidance is known, so attaching it retires that row and
appends a corrected one (`StockStore.revise_print`). The ledger then records
what actually happened — the shortlist was ranked on a row with no guidance,
and a later step supplied it — rather than pretending the row was complete all
along. It does NOT show up in the run's revision report: that report is built
from `detect_revisions`, which compares reported figures only, so a guidance
that arrived late is never described to the reader as the vendor changing its
mind about a number.

Nothing in this file decides anything about the reading, and it deliberately
does not wrap the thing that does: callers reach for `guidance_agent`'s own
`render_request` and `parse_reply`, so there is no second place where the two
modes could drift into accepting different answers. What lives here is the
queue on disk and the one write to the ledger.
"""
from __future__ import annotations

import json
import sys
from typing import Optional

from pydantic import BaseModel, Field

from hawkeye import paths
from hawkeye.contracts.models import new_id, now
from hawkeye.contracts.stocks import next_fiscal_quarter
from hawkeye.scout.guidance_agent import GuidanceExtraction, GuidanceRequest


class GuidanceCase(BaseModel):
    """One print's forward statement, waiting to be read.

    `print_id` is what the reply is attached to. Carrying the id rather than
    re-deriving the row from the ticker and quarter matters: between the scan
    and the submission the vendor can restate a figure, and that appends a new
    row — attaching to whatever is active at submit time would silently move
    the guidance onto a row nobody read the summary for.
    """
    id: str = Field(default_factory=lambda: new_id("gdc"))
    stock_id: str
    print_id: str
    ticker: str
    fiscal_quarter: str
    summary: str

    def request(self) -> GuidanceRequest:
        """What the agent is shown. The quarter the guidance should be about
        is DERIVED rather than stored: two fields holding the same fact are
        two fields that can disagree, and this one is arithmetic."""
        return GuidanceRequest(
            ticker=self.ticker, fiscal_quarter=self.fiscal_quarter,
            next_quarter=next_fiscal_quarter(self.fiscal_quarter),
            summary=self.summary)


# --- the file queue ---------------------------------------------------------

def _case_path(case_id: str):
    return paths.guidance_dir() / f"{case_id}.json"


def save_case(case: GuidanceCase) -> None:
    paths.guidance_dir().mkdir(parents=True, exist_ok=True)
    _case_path(case.id).write_text(case.model_dump_json(indent=2),
                                   encoding="utf-8")


def load_case(case_id: str) -> GuidanceCase:
    return GuidanceCase.model_validate_json(
        _case_path(case_id).read_text(encoding="utf-8"))


def list_cases() -> list[GuidanceCase]:
    """Everything still waiting, by ticker.

    An unreadable file is reported rather than skipped: a queue that quietly
    shrinks looks exactly like a queue that was worked through.
    """
    d = paths.guidance_dir()
    if not d.exists():
        return []
    cases: list[GuidanceCase] = []
    for p in sorted(d.glob("gdc_*.json")):
        try:
            cases.append(GuidanceCase.model_validate_json(
                p.read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"warning: unreadable guidance case {p}: {exc}",
                  file=sys.stderr)
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
        raise ValueError("guidance reply must be a JSON object")
    return raw


def attach(store, case: GuidanceCase,
           extraction: GuidanceExtraction) -> Optional[str]:
    """Put the reading (or the named refusal) on the print row.

    Returns the new row's id, or None when the row the case was staged for is
    no longer the active one — which means a restatement landed in between,
    and the summary this reading came from described the retired row. Refusing
    there is the same rule as everywhere else: a reading whose subject moved
    is not a reading (invariant 6).
    """
    active = store.active_print(case.stock_id, case.fiscal_quarter)
    if active is None or active.id != case.print_id:
        return None
    # A NEW id, because this is a new row rather than an edit of the old one.
    # Reusing the id would collide with the row being retired in the same
    # transaction, and the append-only table would refuse it.
    return store.revise_print(active.model_copy(update={
        "id": new_id("ern"),
        "recorded_at": now(),
        "guidance": extraction.reading,
        "guidance_reason": extraction.reason}))
