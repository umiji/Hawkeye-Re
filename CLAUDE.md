# Hawkeye — notes for Claude sessions

## What this is

Adversarial-verification investment decision system (catalyst-driven US
equities MVP). The core hypothesis and non-negotiables live in
`strategy/INVESTMENT_DOCTRINE.md` and `strategy/VERIFICATION_PROTOCOL.md` — read them
before changing behavior. The user-facing language is Japanese; system code,
docs, prompts, and commit messages are English.

## Communication style (mandatory for every response)

The user reads explanations as a human decision-maker, not as a code
reviewer. This applies to all explanations in this project — bug reports,
behavior descriptions, review summaries, everything.

**Starting premise: assume the reader knows nothing about the code.** Not
the internals, not the processing flow, not the function/table/file names,
not the design concepts, not the domain jargon — nothing. Whatever you have
just read in the source, the user has not read. Nothing is shared context
until you have put it into words in this conversation. Writing as if the
reader already knows is the single most common failure in this project's
explanations, and it makes the answer useless no matter how correct it is.

0. **Every word appearing for the FIRST time in the session gets a short
   gloss — no exceptions.** Not just this project's own vocabulary: code
   symbols (functions, variables, classes), table and column names, file and
   module names, CLI subcommands, external services and APIs, library names,
   and general technical jargon all count. On first mention, say in plain
   Japanese what it is and what it is for, then use the bare name freely for
   the rest of the session. One line is enough — the cost of an unnecessary
   gloss is one line; the cost of a missing one is an explanation the reader
   cannot follow at all, so when in doubt, gloss it.
   - 悪い例: 「earnings_prints に前期のレコードが入っていました」
   - 良い例: 「決算の実績値を1行ずつ貯めておくテーブル（`earnings_prints`）に、
     今回ではなく前四半期の決算のレコードが入っていました」
   - Being asked "それは何？" about a term you already used means this rule
     was broken — apologising is not the fix; glossing on first use is.
1. **Don't lead with bare symbol names.** Avoid dumping function/variable/
   table names as the explanation itself (e.g. "`ensureGaConfigured` 内の
   `isDev` が `false` になる"). The reader can't tell what that means without
   already knowing the code.
2. **Explain the role/purpose first, in plain Japanese, then cite the
   symbol as a parenthetical.** State what the process is *for* and *when*
   it runs before naming it.
   - 悪い例: 「`ensureGaConfigured` 内の `isDev` が `false` になるため...」
   - 良い例: 「GA4の初期化を行う処理（`ensureGaConfigured` 関数）において、
     開発・プレビュー環境であることを識別するフラグ（`isDev` 変数）が
     `false`（本番環境判定）になってしまうため...」
3. **State root cause and user-visible impact**, not just code behavior —
   what changes on screen or in system behavior as a result.
4. **Domain/strategy terms are the highest-risk case of rule 0.** This
   project has its own vocabulary (Bull / Adversary / Judge roles, gates,
   EV hurdle, thesis-accuracy, pre-registration, 審理 vs 審査, etc. — see
   `strategy/INVESTMENT_DOCTRINE.md` and `strategy/VERIFICATION_PROTOCOL.md`).
   These read like ordinary words, so they slip past unglossed more often
   than code symbols do. On first mention give a one-line plain-language
   gloss of what that role/mechanism actually *does* before using it as
   shorthand (e.g. "Bull（強気側の主張だけを作る役割。Adversaryの反論は
   見えない）").

## Invariants (do not break)

1. **Pre-registration**: recommendation payloads in the ledger are immutable.
   Anything that happens later is a journal event. Never add code that
   UPDATEs a recommendation payload.
2. **The journal is hash-chained** — `Ledger.verify_chain()` must stay green.
3. **Code enforces what prompts request**: judge rules (`_judge_rule_check`)
   and risk vetoes (`build_position_plan`) mechanically overturn BUYs.
   If you strengthen a prompt rule, mirror it in code. `_judge_rule_check`
   matches addressed attacks by `Attack.id` (content-hashed in
   `parse_attack_report`, not the LLM's choice) — never re-introduce
   text/substring matching on `attack_statement`; a paraphrased response
   must still count as addressed (2026-07-28 fix, was silently overturning
   correct BUYs).
4. **Information separation**: Bull never sees attacks; Adversary sees only
   the written thesis; Judge sees only the record. Keep the three LLM calls
   stateless and separate. In API mode this is a real technical boundary
   (three independent stateless calls). In session mode it's mechanical only
   for *what each role's input file contains* (`casefile.write_package()`);
   the orchestrating Claude Code session itself has raw filesystem access to
   every role's file and nothing in code stops it from reading ahead — that
   boundary is operational discipline (SKILL.md), not a sandbox, and isn't
   fixable within this architecture (accepted 2026-07-28, see
   `docs/design/MASTER_OVERVIEW.ja.md` §4 and `docs/design/ARCHITECTURE.md`). Don't claim
   session mode has the same technical guarantee API mode does.
5. **No autonomous trading.** The system recommends and records; the user
   executes. Don't add order placement.
6. **Missing data is `unverified`, never a silent pass** (gates). On a hard
   gate this fails closed — `GateReport.hard_failures` counts an unverified
   hard gate the same as a failed one, so the candidate never reaches the
   LLM tribunal on missing data alone (2026-07-28 fix).
7. Doctrine numbers live in `hawkeye/config.py` only. A rule change is a
   config diff with rationale in the commit message.

## Layout

`contracts` (shared models — the only inter-package interface) · `marketdata`
(Yahoo/Finnhub + indicators) · `gates` · `tribunal` (LLM roles + pipeline) ·
`risk` · `ledger` (SQLite store + scoring) · `sentinel` · `reports` (Japanese
rendering) · `cli`.

Directories are split by *who writes the file*: `strategy/` is investment
knowledge a human writes or approves (doctrine, protocol, roadmap, backlog,
drafted revisions), `docs/` is everything Claude and the developer write
about the system (split into `design/` and `knowledge/` below), and
`var/` is everything the system emits at run time (ledger, case files, drop
measurements, reports) and is git-ignored. `hawkeye/paths.py` is the single
place resolving `var/` locations — never hardcode a runtime path elsewhere.
Investment standards do NOT go in `.claude/` (that defines how Claude Code
drives the system; API mode never reads it, and the judgment criteria must
not depend on which engine runs the tribunal).

Three places carry knowledge, and they must not overlap. Cite them by full
path (`docs/design/MASTER_OVERVIEW.ja.md`, never `docs/MASTER_OVERVIEW.ja.md`
— the split happened on 2026-08-03 and the bare form is stale):

- `docs/design/` — design and current state (`MASTER_OVERVIEW.ja.md` is the
  one to read first; `ARCHITECTURE.md`, `DATA_SOURCES.md`, `USER_GUIDE.ja.md`,
  `DEBUG_TOOL.md`, `ARCHITECTURE_REVIEW_BACKLOG.md` sit beside it).
- **`docs/knowledge/` — what outlives a session**, split by WHEN to read it
  (`README.md` is the index):
  - `REJECTED.ja.md` — **read before proposing anything.** Approaches already
    rejected, with the reason. Several are attractive on first sight; the file
    exists so they are not re-discovered.
  - `MEASUREMENTS.ja.md` — **read before quoting a number.** Every figure
    carries its sample, date and method. A figure that is not here has not
    been measured.
  - `TOOLING.ja.md` — read when a command or tool misbehaves.
  - `RETROSPECTIVES.ja.md` — process post-mortems, newest first.
  Do not duplicate what a design doc already states; point at it.
- `~/.claude/session-data/` — what happened in ONE session, in operational
  detail, so the next one can resume. Machine-local, not in git, written by
  `/ecc:save-session`. See "Where the record lives" below.

## Dev

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest          # fully offline; ScriptedLLM + StaticProvider
```

LLM client: `claude-opus-4-8`, adaptive thinking, structured outputs.
Pipeline parsers clamp/normalize all LLM output — keep new LLM fields going
through a parser, never straight into a contract model.

Tribunal prompts stay in `hawkeye/tribunal/prompts.py` — do NOT extract them
to files. A prompt rule and the code enforcing it only mean something
together (invariant 3), and both engines reading the same constant is what
makes API-mode and session-mode results comparable. The readable Japanese
copy at `strategy/TRIBUNAL_ROLES.ja.md` is generated: after editing a role
prompt run `hawkeye docs tribunal-roles --write`, or the test fails. Adding
a numbered Judge rule also requires a gloss in
`hawkeye/reports/tribunal_roles.py` — a rule that binds the Judge must not
be invisible to the reader.

## Session mode (/hawkeye-run)

The tribunal can be driven by a Claude Code session instead of the API:
`.claude/skills/hawkeye-run/SKILL.md` orchestrates `hawkeye case
open/step/submit`, spawning one fresh subagent per role. Invariants 3/4
apply doubly here: `casefile.write_package()` is the only place deciding
what a role sees, and `case submit` runs the same parsers/rule checks as
API mode. Never have the orchestrating session author or edit role JSON —
this instruction, not code, is what keeps you (the orchestrator) from
peeking at another role's raw file; see invariant 4's caveat.

## Receiving a Goal (added 2026-08-02, after a Goal was missed)

When the user states a goal — via `/goal` or in plain words — the FIRST
response must translate it into acceptance criteria before any work starts.
A goal that cannot be checked cannot be reached on purpose, only by luck.

1. **Restate it as 3-5 binary checks, in commands and outputs.** "Confirm X
   works" is not checkable; "`hawkeye scout` runs in production and one
   candidate completes the tribunal with a recorded id" is.
2. **Ask before starting when any of these is true** — one short round trip
   is cheaper than a session spent on the wrong half:
   - a term has more than one plausible reading (this project's own
     vocabulary is a common source: 審理 is the tribunal, 審査 is the
     screening review, and "疑似審査" fits either);
   - the goal bundles several deliverables of different kinds, so "mostly
     done" could pass as done — ask which is the core and which are optional;
   - "done" has no observable form (no command, no output, no file);
   - it cannot be finished in one session — ask what the stopping point is.
3. **Put the acceptance criteria at the top of the todo list**, above the
   implementation phases. Track the goal, not the plan.
4. **Re-read the criteria before saying anything is complete**, and
   demonstrate each one by running the real command (see the 2026-08-02(d)
   retrospective: "348 tests green" was reported as done while the production
   database had none of the new tables).
5. **When work outside the goal turns up** (a real bug, a better design),
   say what it costs and what it delays, then let the user choose. Do not
   silently spend the goal's budget on it.
6. `/goal` installs a **session-scoped** stop condition: it does not survive
   into a resumed session. If a goal is carried over in a hand-off file,
   say so and ask the user to re-issue `/goal` in the new session.

## Governance (added 2026-07-14)

Before any new feature or design change (not small bugfixes/typos), update
`docs/design/MASTER_OVERVIEW.ja.md` §4 (As-Is) and §5 (gap table) — or draft a
short design note — and get user approval BEFORE implementing. This
document was requested after the user flagged that prior sessions
implemented features unilaterally without ever presenting the full
picture (To-Be architecture, As-Is gap, and *why* the design should work)
in one place. Keep §4/§5 current as capabilities land.

## Where the record lives (there is no session log in this file)

This file is loaded in full at the start of every session, so it holds only
what has to be read every time. Session history is deliberately NOT here:

- **To resume work** — `~/.claude/session-data/<date>-<id>-session.tmp`,
  written by `/ecc:save-session` and read by `/ecc:resume-session`. Local to
  the machine, not in git, shared by every project on it. It carries
  operational state: what worked, what failed and why, the exact next step.
- **Lessons that outlive a session** — `docs/knowledge/` (see Layout above).
  Git-tracked, read on the trigger its README states. Anything from a session
  that still matters a month later belongs there, not in a log.
- **The old hand-off entries (2026-07-12 .. 2026-08-03)** — moved verbatim to
  `docs/knowledge/HANDOFF_ARCHIVE.ja.md`. Nothing was discarded; it simply
  stopped being loaded into every session, where it had grown to 77% of this
  file.
