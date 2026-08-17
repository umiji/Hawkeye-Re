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
2.5. **目的は「最終的に何のためか」まで書く。処理上の目的で止まるな。**
   裏側の処理を説明するとき、「順位付けをするため」「データを取り込むため」
   「点数を計算するため」は**手段であって目的ではない**。それは処理の内部を
   知っている人間にしか意味がなく、読み手には何のために存在する処理なのかが
   渡らない。**その処理が最後に何に効くのか —— 誰が、いつ、そのデータを何の
   判断に使うのか —— まで書いて、はじめて目的を書いたことになる。**

   - 悪い例: 「決算の見通しを取り込むための処理です」
   - 良い例: 「決算の審理（Bull / Adversary / Judge の3役が1銘柄を争う工程）で、
     3役が『会社が自分で示した来期の見通し』を読んで買う・買わないの判断材料に
     するための、そのデータを取り込む処理です」
   - 悪い例: 「銘柄を順位付けする工程です」
   - 良い例: 「1日に数百件出てくる決算のうち、どれを審理にかけるか（＝Userが
     最終的に買うか判断する候補にするか）を決めるために、銘柄を並べ替える工程です」

   目安: 説明の中に「そのデータを**誰が読んで**、**何を判断する**のか」が
   1回も出てこなければ、最終目的を書いていない。書き直す。

3. **State root cause and user-visible impact**, not just code behavior —
   what changes on screen or in system behavior as a result.
3.5. **Write subject–verb–object. Name the actor.** "判定が書かれる" tells the
   reader nothing: who writes it, into what, when? Say "走査が、入口ゲートの
   判定直後に、銘柄マスタ（`stocks` テーブル）の3つの列に書く". Passive voice
   with the actor dropped is the single most common way an explanation in this
   project becomes unreadable.

3.6. **Never reuse a bare noun that has several referents in this system.**
   The words 走査 / 足切り / スクリーン / ゲート / 判定 / 候補 / マスタ /
   落選記録 all name more than one thing if left unqualified. Say WHICH one,
   every time, even at the cost of repetition. The reader should never have to
   ask "どのゲートの話？".

   Concrete failures from 2026-08-08, all of which forced the user to ask again:
   - 悪い: 「ゲートに到達した銘柄だけ」→ 良い: 「入口ゲート（審理に送ってよいか
     を判定する7条件）まで到達した銘柄だけ」
   - 悪い: 「トリアージ判定」→ 良い: 「会社そのものを対象外にする判定（入口
     ゲート7条件のうち、株価・時価総額・売買代金の3つだけを見たもの）」
   - 悪い: 「numbers_reason を候補と落選記録に載せた」→ 良い: 「EWが数字を出せ
     なかった理由を、走査中の作業用データと、台帳に永久保存される
     `screened_candidates` テーブルの両方に、銘柄ごとに書くようにした」
   - 悪い: 「マスタ870件のうち判定を持つのは2件」→ この文は、マスタが何か・
     870がどこから来た数字か・2が候補数ではないことを全部説明しないと通じない。

3.7. **Show the real artifact instead of describing it.** When the user asks
   what something is, print the actual JSON field, the actual sentence, the
   actual row. One `summary` excerpt from a fixture explained "ガイダンス" in
   a way three paragraphs of prose had failed to.

3.8. **State the limits of what you built, unprompted.** On 2026-08-08 a
   feature was wired in and described as useful; the user's questions exposed
   that it saves one API call per run at best and probably never fires. That
   should have been said when it was built, not extracted. If a mechanism has
   a condition that makes it rarely fire, that condition IS the headline.

3.9. **不具合・挙動の報告は、下の3ブロックで書く（2026-08-17 改訂）。**
   二つを禁止する。**(a) コード上の名前だけで説明すること。(b) 起きている現象だけ
   述べて終わること。** 「順位付けに反映されない」「処理が落ちる」は現象であって
   報告ではない。**その現象によって User とプロジェクトの目的に何が起きるかまで
   書き切って、はじめて報告になる。** 空欄のまま出してよいブロックは無い。

   ```
   概要（3文以内。1文1役割。これを超えたら詳細ブロックへ移す）:
   1文目 <どの工程で> <何が起きているか>
   2文目 そのため <後続の処理／出力> が <どうなるか>
   3文目 結果 <User とプロジェクトに何が不都合か> —— 誤った判断／分析ができない／
         ハルシネーションのリスク、そして金銭判断に効くのか見え方だけなのか

   問題の発生領域とその問題:
   ・どこで・どんなシーンで: <コマンド> の <フェーズ> の <どの処理> で発生
   ・本来 <こうあるべき> が、現状は <こうなっている>。<どの条件で起きるか>
   ・頻度: <実測値・標本・日付>。測っていないなら「未測定」と書く
   ・これにより <次のどの工程> が <何を誤り>、<出力／判断がどう歪むか>

   対策案と対策案実装時の影響:
   ・<案A: 何をどう変えるか> / <直る範囲・副作用・他機能への波及・コスト>
   ・<案B: ...>
   ```

   守り方:

   - **コードの名前を裸で置くな（規則 0〜3 の再掲。最も破られる）。** 関数名・変数名・
     テーブル名・ファイル名・CLI サブコマンドを、それ単体で説明にしてはいけない。
     「何のための処理か」「どんな役割の値か」を平易な日本語で先に書き、名前は
     括弧で添える。読み手はスクリプトの中身を一切知らない前提。
   - **概要は現象で終わるな。** 「Xが反映されない」で止まった報告は差し戻し。
     「反映されないので、審理3役が◯◯を判断できず、△△という誤りが起きる」まで書く。
   - **壊れている処理の「最終目的」を必ず添える（規則 2.5）。** 「順位付け工程で
     問題が起きた」だけでは、それが壊れると何が困るのかが読み手に渡らない。
     その処理が最後に何に効くのか（誰がそのデータを読んで、何を判断するのか）を
     書けば、影響の記述は自然に埋まる。
   - **概要は3文まで。長い概要は概要ではない。** 発生条件・数値・経緯・実測値・
     関数名は概要に入れない —— それは全部、下の詳細ブロックの仕事である。概要で
     読み手に渡すのは「どこが壊れていて、自分にとって何が困るか」だけでよい。
   - **影響は「誰にとって」を明示する。** User 本人の意思決定か、審理の質か、
     プロジェクトの目的（勝てる銘柄の選定）か。そして金銭判断に効くのか、
     見え方だけなのかを、概要の中で先に言う。
   - **概要に主語を置く。** 「反映されていなかった」は誰がやらなかったのかが無い。
     「順位付け工程が、〜を〜に反映していない」と書く。
   - **格好をつけた言い回しを使うな。** 「識別子」「フェイルクローズ」「埋もれる」は、
     書き手が要点を掴んだ気分になるだけで、読み手には何も渡らない。
   - **実物を1行そのまま引用する（規則 3.7）。** 画面に出ていた文字を要約しない。
     要約した瞬間、読み手はそれが本当に問題なのか検証できなくなる。
   - **対策案は必ず影響とセットで書く。** 直る範囲だけでなく、何が壊れうるか、
     どの処理に波及するか、コストはどれだけか。未測定なら「未測定」と書く。

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

`.claude/skills/hawkeye-run/SKILL.md` owns the procedure and Ground rules
for session mode (information separation, never author or edit role
output, never bypass `hawkeye case submit`) — read it before touching
orchestration, not this file. Invariants 3/4 above apply doubly here; the
code boundary is `casefile.write_package()`.

## Receiving a Goal — Hawkeye addendum (added 2026-08-02)

The global acceptance-criteria rule (`~/.claude/rules/common/goals.md`)
applies in full here: translate a stated goal into 3-5 binary checks
before starting, ask before proceeding when a term is ambiguous or "done"
has no observable form, track the goal (not the plan) at the top of the
todo list, and re-read the criteria — by running the real command — before
claiming anything is complete. Two things specific to this project:

- Vocabulary is a common source of ambiguity here: 審理 is the tribunal,
  審査 is the screening review, and "疑似審査" fits either reading — ask
  which is meant rather than picking one.
- 2026-08-02(d): "348 tests green" was reported as done while the
  production database had none of the new tables. A green test suite is
  evidence about logic, not about the delivered path.

## Governance (added 2026-07-14)

Before any new feature or design change (not small bugfixes/typos), update
`docs/design/MASTER_OVERVIEW.ja.md` §4 (As-Is) and §5 (gap table) — or draft a
short design note — and get user approval BEFORE implementing. This
document was requested after the user flagged that prior sessions
implemented features unilaterally without ever presenting the full
picture (To-Be architecture, As-Is gap, and *why* the design should work)
in one place. Keep §4/§5 current as capabilities land.

## Task intake gate (added 2026-08-15)

`docs/task-list-hawkeye-re.md` is the single source of truth for progress;
work one task at a time. **Before starting any new task request — even a
casual one-liner — check it against `docs/task-template.md`'s 7 fields**
(ID/title, purpose, scope, prohibitions, completion criteria, test plan,
stop conditions). If any are missing or ambiguous, ask the user in a
bulleted list before writing any code. Once confirmed, add the formal
entry to `docs/task-list-hawkeye-re.md` and get agreement before branching
or implementing.

The detailed per-task cycle (implement → test → update task-list →
commit/Draft PR → evidence-backed report), the standing prohibitions, and
the stop conditions live in the `hawkeye-task-cycle` skill
(`.claude/skills/hawkeye-task-cycle/SKILL.md`) — read it once a task is
confirmed and you're about to start work.

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
