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
5. **The same two rules bind both extraction steps** (2b, 2b-2), which are
   agent steps OUTSIDE the tribunal. You never write their JSON, and neither
   reader sees the figure its reading will be measured against or asked to
   explain. Each reply goes through its own `submit` command, which is where
   the quote is checked against the source text — the only hallucination
   check there is.
6. **You never write a reader's instructions either.** Both queue commands
   emit a `system` file; hand the subagent that file's contents verbatim. It
   holds the same text the metered API path sends, and that is the only
   reason the two engines' answers can be compared at all. Writing your own
   version once cost a reading outright (AMBQ, 2026-08-18: the improvised
   instruction named a unit the gate does not accept).

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

The `--case-id` form names four files, exactly as `hawkeye case step`
does for the tribunal roles:

```
system:         <...>/guidance.system.md   what the reader is told
input:          <...>/guidance.input.md    what it reads
schema:         <...>/guidance.schema.json the shape its reply must take
write_reply_to: <...>/guidance.out.json    where its reply goes
```

**Spawn a fresh subagent and give it the contents of `system` and `input`,
verbatim.** Do not summarise them, do not add to them, and above all do not
write your own version of the instruction — `system` is the same constant the
metered API path sends, and a run driven by different words cannot be compared
with one driven by these. Its whole job is to copy one range out of one
sentence. Write its reply to `write_reply_to` **unmodified**, then:

```bash
hawkeye guidance submit <case_id> --file <write_reply_to> --reader <model>
```

The submit prints the quarter's three legs again, because the guidance leg
is the only one that can still move at this point.

**One subagent per case, and it sees only the package.** It must never be
shown the consensus figure its reading will be measured against — an
extractor that can see the bar it is about to clear has stopped
extracting. `hawkeye guidance queue` already withholds it; do not add it.

**Two kinds of refusal, and they end differently.** A company that guided
nothing is a normal outcome, is recorded by name, and clears the queue —
never retry that by rewording the request, and never write the JSON yourself.
A reply that fails a MECHANICAL check (the quote is not in the source, the
period is unreadable) is OUR failure: the command exits non-zero, says so, and
**leaves the material staged**. Re-read the same `system` file, give it to a
fresh subagent, and submit against the same case id.

If scout passes zero candidates, report the funnel numbers honestly and
stop: **no catalyst means no trade — do not go hunting for one.**

### 2b-2. Read why the quarter came out where it did

The scan stages the SAME summary a second time, for a different question.
Almost every candidate here has one shape — a large EPS surprise beside a
small revenue one — and that shape means either an item that will not repeat
(a tax effect, a gain, a settlement) or a margin the company earned. Nothing
in the numbers separates them, so before this existed the roles guessed and
wrote the guess down as fact. Work this queue the same way:

```bash
hawkeye cause queue                          # what is waiting
hawkeye cause queue --case-id <id>           # ONE package
```

The `--case-id` form names the same four files (`cause.system.md`,
`cause.input.md`, `cause.schema.json`, `cause.out.json`). **A fresh subagent
per case; give it `system` and `input` verbatim and write its reply to
`write_reply_to` unmodified.** Its whole job is to copy one sentence out of
one summary. The instruction it needs is already written — including the four
unit names the gate accepts, which are `per_share`, `million`, `billion` and
`percent` and nothing else. Do not compose an instruction of your own.

```bash
hawkeye cause submit <case_id> --file <write_reply_to> --reader <model>
```

The same information rule binds this reader, and harder: **it must never be
shown the surprise it is being asked to explain.** Told "EPS beat by 20%
while revenue was flat, why?", an extractor has been handed the premise that
a reason exists — and a reason that was never in the source is exactly the
failure this step was built to stop. `hawkeye cause queue` withholds the
figures; do not add them back, and do not "help" by naming the shape you
noticed in the numbers.

`"explained": false` is a common and correct answer: the release states no
reason, it is recorded by name, and the queue clears. Do not retry that by
rewording the request. A mechanical failure behaves as in 2b — non-zero exit,
material kept, same case id, fresh subagent.

Order does not matter between this queue and 2b, and unlike 2b **nothing
waits on this one**: the reading changes no score, so `hawkeye rank` does
not depend on it. Drain it anyway before opening cases — a candidate argued
without it reaches the tribunal with its cause marked UNVERIFIED, which is
honest but is the whole thing this step exists to avoid.

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
saved as `var/reports/<yymmdd-HHMMSS>-<TICKER>-tribunal-report.md` (printed
path follows as "レポート保存先: ..."). The ticker is in the name so two rounds
finishing in the same second cannot land on one file, and a name already taken
gets `-2` appended rather than being overwritten (T-017). This also happens for
a gate-only rejection in step 2 (before the tribunal ever runs), where the
report is just the gate-failure rationale instead of a verdict.

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
