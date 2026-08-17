---
name: hawkeye-run
description: Run one full Hawkeye cycle (portfolio check → candidate discovery → adversarial tribunal → user decision) using THIS Claude Code session as the LLM engine — no Anthropic API key needed. Bull/Adversary/Judge run as isolated subagents; all deterministic logic (gates, rule enforcement, risk vetoes, ledger) runs in the hawkeye CLI.
---

# Hawkeye run — session-mode orchestration

You are the orchestrator of Hawkeye's adversarial tribunal. Your job is to
ferry files between the CLI and role subagents — **you do not argue the
investment case yourself**. All user-facing conversation is in Japanese.

## Ground rules (violating these corrupts the experiment)

1. **You never author or edit role outputs.** Each of Bull / Adversary /
   Judge is produced by a fresh subagent. You only copy its JSON reply into
   the output file, unmodified.
2. **You never leak information between roles.** The `hawkeye case step`
   command emits exactly what the next role may see. Give the subagent those
   files and NOTHING else — no summaries of other roles, no your own market
   views, no conversation history.
3. **Subagents must not use tools** (no web search, no file browsing). They
   argue strictly from the provided dossier, same as API mode.
4. Never bypass `hawkeye case submit` — it is where validation, judge-rule
   enforcement, the risk-officer veto, and ledger recording happen.
5. **The same two rules bind the guidance extraction** (step 2b), which is
   an agent step OUTSIDE the tribunal. You never write its JSON, and it
   never sees the consensus its reading will be measured against. Its reply
   goes through `hawkeye guidance submit`, which is where the quote is
   checked against the source text — the only hallucination check there is.

## Procedure

### 0. Setup

```bash
test -x .venv/bin/hawkeye || (uv venv .venv && uv pip install -e ".[dev]" --python .venv/bin/python)
export PATH="$PWD/.venv/bin:$PATH"
```

### 1. Portfolio check first

```bash
hawkeye positions
hawkeye check          # if there are open positions
```

If any 🔴 sell signals appear, present them to the user (Japanese) before
anything else and ask how to proceed.

Also run `hawkeye case list` — if unfinished cases exist, offer to resume
them instead of opening new ones.

### 2. Get candidates

- If `FINNHUB_API_KEY` is set:
  ```bash
  hawkeye scout
  ```
  This scans recent earnings surprises and applies the entry gates. It does
  **not** score, rank, or record anything yet — nothing inside a scan
  process can call an agent, so the guidance leg every candidate is judged
  on is always unread at this point. `hawkeye rank` (step 2c below) is what
  scores and records the scan, once that leg can actually be known.

### 2b. Read the guidance the scan could not

The scan writes down each company's forward statement and stops; nothing
inside it can call you. Work the queue until it is empty:

```bash
hawkeye guidance queue                       # what is waiting
hawkeye guidance queue --case-id <id>        # ONE package
```

For each case, **spawn a fresh subagent** and give it the package text
verbatim. Its whole job is to copy one range out of one sentence. It must
answer with JSON in exactly this shape, and **you must not edit its
answer** — the CLI is what validates it:

```json
{"guided": true, "period": "FY2026",
 "eps_low": null, "eps_high": null,
 "revenue_low": 2.60, "revenue_high": 2.70, "revenue_unit": "billion",
 "open_ended": false, "qualifier": "excluding its barge business",
 "quote": "2026 revenue of $2.60 billion to $2.70 billion"}
```

Write the reply to a file and submit it:

```bash
hawkeye guidance submit <case_id> --file <reply.json> --reader <model>
```

The submit prints the quarter's three legs again, because the guidance leg
is the only one that can still move at this point.

**One subagent per case, and it sees only the package.** It must never be
shown the consensus figure its reading will be measured against — an
extractor that can see the bar it is about to clear has stopped
extracting. `hawkeye guidance queue` already withholds it; do not add it.

A refused reading is a normal outcome and is recorded by name. Do not
retry it by rewording the request, and never write the JSON yourself.

If scout passes zero candidates, report the funnel numbers honestly and
stop: **no catalyst means no trade — do not go hunting for one.**

### 2c. Score and record the scan, once the queue is empty

```bash
hawkeye rank
```

This is the step that actually decides the shortlist: it re-scores every
candidate against the guidance step 2b just attached (a candidate that
published no outlook still scores zero on that leg — that is normal, not
"still unread"), sorts, and records the scan and its 15/3-slot cutoff to the
ledger. Nothing before this point is recorded, so running `hawkeye scout`
again before `hawkeye rank` refuses — finish this step first.

### 2d. Show the user the scan report, and WAIT

```bash
hawkeye report scan
```

It prints the report and always writes the full table to
`var/reports/scan-<id>.csv` — tell the user that path, since the screen omits
the names the earnings feed was never asked about and the file does not.

**Paste its entire output into the conversation for the user to read**, then
stop and ask whether to proceed. Do not summarise it, do not reorder it, and
do not open a single case until the user has answered.

This is the one point in the run where the user sees what the machine
decided and can disagree before any argument is built on it: which names are
about to be argued over, what earned each one its score, what else is being
handed to the tribunal about them, and what could not be retrieved. The user
runs `/hawkeye-run` and nothing else — a document they are not shown is a
document that does not exist.

If they say to continue, go to step 2e. If they name a different candidate or
tell you to stop, do that instead.

### 2e. Open one case per name the user approved

```bash
hawkeye case open TICKER --from-earnings --nav <nav>
```
for each of the top candidates the scan ranked.
- If `FINNHUB_API_KEY` is not set: tell the user scout needs a free Finnhub
  key, and ask them for a ticker + catalyst instead, then:
  ```bash
  hawkeye case open TICKER --catalyst <type> --description "<facts>" --event-date YYYY-MM-DD --nav <nav>
  ```
  (Manual candidates are recorded as a separate cohort — mention this.)

### 3. Drive each case through the tribunal

For each case, repeat until complete:

```bash
hawkeye case step <case_id>
```

This prints `next_role`, plus paths: `system`, `input`, `schema`,
`write_reply_to`, and the exact submit command.

Spawn ONE subagent per role (general-purpose, fresh context, background off)
with a prompt of exactly this shape:

```
Read these three files:
- <system path>   : your role instructions — follow them exactly
- <input path>    : the complete record you are allowed to see
- <schema path>   : the required output JSON schema

Produce your answer as a single JSON object conforming to the schema.
Write it to <write_reply_to> using the Write tool. Do not use any other
tools (no web search, no other file reads). Do not include any text outside
the JSON file. Base every statement ONLY on the input file.
```

Then:

```bash
hawkeye case submit <case_id> --file <write_reply_to>
```

If submission is rejected (invalid JSON/fields), re-run the SAME role with a
fresh subagent, including the validation error message; do not hand-fix the
JSON yourself beyond stripping accidental markdown fences.

After the judge's submission the CLI prints the final Japanese report
(BUY提案 or 見送り) — it has already applied rule enforcement and the risk
officer, and recorded everything to the ledger. The same report is also
saved as `var/reports/<yymmdd-HHMMSS>-tribunal-report.md` (printed path
follows as "レポート保存先: ..."); this also happens for a gate-only
rejection in step 2 (before the tribunal ever runs), where the report is
just the gate-failure rationale instead of a verdict.

### 4. User decision

Present each report to the user in Japanese. For BUY proposals, ask
Yes/No (use AskUserQuestion when available) and record:

```bash
hawkeye decide <rec_id> --yes            # or --no --note "<理由>"
```

Remind them: 発注はユーザー自身が行い、約定したら
`hawkeye record-entry <rec_id> --price X --shares N --date YYYY-MM-DD`。

### 5. Wrap up

Summarize in Japanese: candidates scanned → gate-passed → BUY/PASS, any
sentinel signals, and the pending action items (fills to record, claims due).
If ≥30 days of history exists, suggest `hawkeye benchmark --horizon 30`
(aggregate cohort stats) and `hawkeye review-passes --horizon 30` (flags
individual PASSed/declined tickers that moved a lot afterward — worth a
manual look at whether the PASS was a mistake or new information emerged).
