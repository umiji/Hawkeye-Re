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
  hawkeye scout --open-cases 3
  ```
  This scans recent earnings surprises, applies gates, and opens session
  cases for the top candidates (funnel counts are recorded automatically).
- If not set: tell the user scout needs a free Finnhub key, and ask them for
  a ticker + catalyst instead, then:
  ```bash
  hawkeye case open TICKER --catalyst <type> --description "<facts>" --event-date YYYY-MM-DD --nav <nav>
  ```
  (Manual candidates are recorded as a separate cohort — mention this.)

If scout passes zero candidates, report the funnel numbers honestly and
stop: **no catalyst means no trade — do not go hunting for one.**

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
officer, and recorded everything to the ledger.

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
