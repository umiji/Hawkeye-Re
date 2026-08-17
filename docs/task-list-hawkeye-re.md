# Task List

進捗管理の唯一の正本（single source of truth）。新規タスクの受付・登録手順は
`hawkeye-task-cycle` Skill（`.claude/skills/hawkeye-task-cycle/SKILL.md`）と
`docs/task-template.md` を参照。1タスクずつ進行し、完了条件を満たすまで
次のタスクに着手しない。

## ID 採番ルール

- 形式は `T-###`（3桁ゼロ埋め、連番）。
- 一度発行した ID は再利用しない。中止・保留になったタスクも欠番のまま
  この表に残し、削除しない。

## カラムの使い分け

- **タスク名**: 10〜40文字程度。そのタスクを端的に説明したものにする
  （`docs/task-template.md` 項目1）。
- **タスク詳細**: 目的・変更範囲・禁止事項など（`docs/task-template.md`
  項目2〜4）をまとめて記述する。長文になってよい。
- **依存**: 他タスクへの依存（`docs/task-template.md` 項目8）。
  `T-001（ブロッカー）`/`T-001（推奨: 理由）`の書式で書く。依存が無ければ
  「無し」と明記し、`-`や空欄のままにしない。

## 状態の選択肢

`未着手` / `調査中` / `実装中` / `ローカル検証済み` / `実環境検証待ち` /
`完了` / `保留` / `中止`

## タスク一覧

| ID | タスク名 | タスク詳細 | 状態 | 進捗 | 依存 | 完了条件 | 証拠 |
|----|----------|------------|------|------|------|----------|------|
| T-000 | (サンプル行) 記載形式の例 | タスクの記載形式の例。実タスク登録時に置き換えるか、新規行を追加する | 未着手 | 0% | - | 完了条件をここに具体的なコマンド/出力で明記する | - |
| T-001 | Judgeルール3・5をEVベースに一本化 | （`docs/design/RETURN_TARGET_STRUCTURAL_GAPS.ja.md` 障害C対応）。**目的**: ルール3（Adversaryの短い主張がBullの長い主張より説得力があればPASS、という弁論の勝敗ルール）が、severity4以上の攻撃を監視条件に転換してもなお機械的にPASSへ誘導しており、実績19件全件PASSという極端な結果になっている。放置すると審理から一切BUYが出ず、年50%目標の達成が構造的に不可能。**変更範囲**: `hawkeye/tribunal/prompts.py`（JUDGE_SYSTEM: ルール3を独立ルールとして撤廃し、「未反証だが転換済みのseverity4以上の攻撃は確信度から相応の割引を行う」という指示にルール5側で統合）／`hawkeye/tribunal/pipeline.py`（`_judge_rule_check`の確信度閾値を0.55→0.65に変更。ロジック自体は変えず定数のみ）／`strategy/VERIFICATION_PROTOCOL.md`（ルール3・5の記述更新、「enforced twice」の記述精度も見直す）／`hawkeye docs tribunal-roles --write`によるドキュメント再生成。**禁止事項**: ルール1（デフォルトPASS）・ルール2（severity4以上の攻撃の未対応チェック）・ルール4（Risk Officerの経済性チェック）はスコープ外。`config.py`の他の閾値（`min_reward_risk`等、障害A関連）は触らない。 | 実環境検証待ち | 100% | 無し | ① `prompts.py`のJUDGE_SYSTEMからルール3の弁論勝負の文言が消え、確信度割引の指示に置き換わっている ② `_judge_rule_check`の確信度チェックが0.65を使っている ③ `hawkeye docs tribunal-roles --check`が通る ④ 既存tribunalテストスイートが全件成功する ⑤ 新設計の挙動を検証する新規テスト（severity4攻撃を転換した上で確信度0.65超→BUY、0.65以下→PASSの両ケース）が追加され成功する<br>**テスト方法**: 単体（ScriptedLLM/StaticProviderでのオフラインテスト）。実環境確認は次回`/hawkeye-run`実行時にJudgeの実際のconviction分布を観察（即時検証不可のため継続観察）。<br>**停止条件**: CLAUDE.mdの共通停止条件のみ。 | 2026-08-17実装。①〜⑤すべて充足。`hawkeye/tribunal/prompts.py`（JUDGE_SYSTEMのルール3を撤廃しルール4へ統合、旧ルール4→3・旧ルール5→4・旧ルール6→5に繰り上げ）／`hawkeye/tribunal/pipeline.py`（`_MIN_BUY_CONVICTION = 0.65`を新設し`_judge_rule_check`が参照）／`hawkeye/reports/tribunal_roles.py`（日本語解説の番号振り直しとルール4の記述差し替え）／`strategy/TRIBUNAL_ROLES.ja.md`（`hawkeye docs tribunal-roles --write`で再生成、`--check`が「✅ prompts.py と一致」を出力）／`strategy/VERIFICATION_PROTOCOL.md`（「enforced twice」→「5つのうちコードで再検算するのは2つだけ」に訂正、旧ルール撤廃の経緯を追記）／テスト4本を新設（`test_converted_severe_attack_keeps_buy_above_the_floor` / `..._overturns_buy_below_the_floor` / `test_conviction_that_cleared_the_old_floor_no_longer_buys` / `test_conviction_exactly_at_the_floor_still_buys`）。`.venv/Scripts/python.exe -m pytest -q` → **778 passed**（失敗0）。commit: 820d30c。**未検証**: 実環境でのJudgeのconviction分布は次回`/hawkeye-run`実行時に継続観察（本タスクでは検証不可）。 |
| T-002 | セクターETF比較材料を審理に追加 | （セッション方式のみ、`docs/design/RETURN_TARGET_STRUCTURAL_GAPS.ja.md` 障害F対応）。**目的**: 審理3役に業界・セクターの比較材料が一切無く、銘柄固有の反応か業界全体の流れかを区別できない。**変更範囲**: 新規セクター→ETF変換表（主要セクターのみ、GICS11セクター相当）／既存OHLCV取得（Yahoo/Finnhub）を再利用したETF値動き取得処理／`hawkeye/contracts/models.py`（`CandidateBrief`への新フィールド追加）／`hawkeye/scout/`配下のdossier組み立て箇所への配線／`hawkeye/tribunal/prompts.py`（新フィールドの説明追記）／`hawkeye/tribunal/casefile.py`（セッション方式のパッケージ出力への反映）。**禁止事項**: 今回はセッション方式のみ対応、API方式（`hawkeye/tribunal/llm.py`経由）への配線はスコープ外（`CandidateBrief`拡張自体はモデル共有により自然に波及しうる点に留意）。主要セクター以外（Finnhubの細かい業種分類）への個別対応はスコープ外。既存`CandidateBrief`の他フィールドの意味は変えない。 | 未着手 | 0% | T-001（実装のブロッカーではないが、ルール3が直っていないと新しい材料を渡しても効果が判定できないため、着手順としてT-001を先に推奨） | ① 主要セクター（GICS11セクター相当）のETFマッピング表が実装されている ② `CandidateBrief`に新フィールドが追加され、`casefile.write_package()`のセッション方式dossier出力に反映されている ③ 新規ユニットテスト（セクター名→ETFコード変換、ETF値動き計算）が追加され全件成功する ④ 既存テストスイートが全件成功する<br>**テスト方法**: 単体（変換関数・計算関数）＋結合（`build_brief`からdossier出力までの一連の流れ）。実環境確認は次回`/hawkeye-run`実行時。<br>**停止条件**: CLAUDE.mdの共通停止条件に加え、Finnhubの`finnhubIndustry`分類が主要セクターのどれにも当てはまらない業種が多数見つかった場合、対応方針をUser確認。 | - |
| T-003 | 決算解説文から乖離原因を抽出し検証付きで渡す | （`docs/design/RETURN_TARGET_STRUCTURAL_GAPS.ja.md` 障害D-2の前提／「参考」節のハルシネーション対策）。**目的**: EPSサプライズと売上サプライズが大きく乖離した決算について、その原因（一時的な税効果・評価益なのか、本物のマージン改善なのか）を審理3役が検証する手段が無く、Judge/Adversaryは推測をそのまま事実であるかのように書いている（PGY・UNHの実例で確認済み）。障害D-2を正しい順序（原因分析→OK/NG判定）で実装するための前提であり、放置するとD-2は「理由を見ずに形だけで機械的に減点する」という誤った設計のまま実装されるリスクがある。**変更範囲**: 新規`hawkeye/scout/quality_extraction_agent.py`（仮。`guidance_agent.py`と同型のパターン——EarningsWhispersの`summary`全文を入力、原因に言及した引用文を抽出、4つのハルシネーション防止チェック［引用が原文に実在するか・正しい文脈での引用か・単位が明記されているか・期間が一致しているか］を適用）／`hawkeye/scout/quality.py`（`LegVerdict.excerpt`をEPS/売上レッグにも適用する配線。ガイダンスレッグの既存パターンをそのまま流用）／`.claude/skills/hawkeye-run/SKILL.md`（セッション方式でこの抽出をどのタイミングで走らせるか。ガイダンス抽出と同様の非同期キュー方式が要るか検討）／`hawkeye/tribunal/prompts.py`（新しい`excerpt`をJudge/Adversaryがどう扱うかの指示追記。既存のnull≠zeroの原則をそのまま適用し、原因抽出が無ければ`unverified`として扱う）。**禁止事項**: 決算資料本体（プレスリリース・10-Q等）の新規取得はスコープ外（障害B3として別途検討済み・優先度低）。抽出対象は必ずEarningsWhispersの`summary`のみ。抽出結果でEPS/売上の実績値・サプライズ率を書き換えることは禁止（不変条件1・不変条件6に抵触。あくまで理由の注記であり、数値の訂正ではない）。 | 未着手 | 0% | T-001（実装のブロッカーではないが、ルール3が直っていないと新しい材料を渡しても効果が判定できないため、着手順としてT-001を先に推奨） | ① 新規抽出エージェントが実装され、`guidance_agent.py`の`parse_reply()`と同等の4項目チェックを持つテストが全件成功する ② EPS/売上レッグの`LegVerdict`に`excerpt`が設定される経路が、ガイダンスレッグと同じ形で動作することを確認する単体テストが追加され成功する ③ 抽出結果が無い場合に`unverified`扱いとなり、Judge/Adversary側のプロンプトが「未確認」として扱う指示になっていることを確認するテストが追加され成功する ④ 既存の`test_earnings_quality.py`等の関連テストスイートが全件成功する<br>**テスト方法**: 単体（引用検証ロジック、ScriptedLLMでの疑似応答に対する4項目チェック）。結合（`quality.py`全体の判定フローにexcerptが正しく乗ること）。実環境確認は次回`/hawkeye-run`実行時。<br>**停止条件**: CLAUDE.mdの共通停止条件に加え、セッション方式での実行タイミングが既存のSKILL.md手順と矛盾する場合は実装前にUser確認。 | - |
| T-004 | Overreaction銘柄スキャナーを新設し審理に統合 | （`docs/design/RETURN_TARGET_STRUCTURAL_GAPS.ja.md` 障害B対応）。**目的**: ドクトリンが認める4種のエッジ源のうち`overreaction`（一時的な問題で質の良い銘柄が過剰に売られる）を検出する仕組みが存在せず、決算サプライズだけでは狙える機会が絞られている。**設計方針（Claude提案・User承認済み）**: 統合方法＝`/hawkeye-run`の日次サイクルに組み込み、決算候補と同じ審理枠を使う。審理枠配分＝1日3枠のうち1枠をoverreaction候補に予約（該当日に入口ゲート通過候補が無ければ3枠とも決算候補に戻す。新エッジ源のデータ蓄積を優先し複雑な横断スコア正規化はしない）。急落の条件＝直近10営業日（決算カタリストの鮮度条件と数字を揃えた）で株価15%以上下落、かつ下落期間の平均出来高が過去3か月平均の1.5倍以上（暫定値。実測データが貯まり次第見直す）。原因特定の基準＝下落期間内に否定的なニュースが見つかった候補のみを対象とし、理由不明の下落はv1では対象外（ドクトリンの`overreaction`定義が「直せる、定量化できる問題」を前提とするため）。既存ソフトゲート＝ATR14≤8%・次回決算7日超はそのまま流用、「決算当日の値動き≤25%」は「下落期間全体の下落率50%超で除外」に置き換え。**変更範囲**: 新規`hawkeye/scout/overreaction.py`（仮。既存OHLCV取得・ニュース取得を再利用した急落検出＋原因特定＋順位付け）／既存の入口ハードゲート（価格・時価総額・ADV20）をそのまま適用する配線／`.claude/skills/hawkeye-run/SKILL.md`（日次サイクルへの組み込み、審理枠1枠の予約ロジック）／候補生成時に`CatalystType.EARNINGS_OVERREACTION`（既存enum）を割り当てる配線。**禁止事項**: API方式への配線はスコープ外（T-002と同様、セッション方式のみ）。決算サプライズ側の既存ロジック（ランキング式・入口ゲート等）は変更しない。急落条件・原因特定基準の数値は実測データが無いまま追加調整しない（暫定値のまま実装し運用データが貯まってから見直す）。 | 未着手 | 0% | T-001（実装のブロッカーではないが、ルール3が直っていないとoverreaction候補が本当に良いエッジ源か判定できないため、着手順としてT-001を先に推奨） | ① 急落検出ロジック（15%下落・10営業日・出来高1.5倍）が実装され単体テストが成功する ② ニュース突合による原因特定ロジックが実装され、原因が見つからない候補が除外されることを確認するテストが成功する ③ 既存ハードゲートと新設ソフトゲート（下落率50%上限・ATR14・次回決算7日）が正しく適用されることを確認するテストが成功する ④ `/hawkeye-run`の日次サイクルでoverreaction候補が1枠を予約し、候補が無い日は決算候補が3枠とも埋めることを確認するテストが成功する ⑤ 既存テストスイートが全件成功する<br>**テスト方法**: 単体（急落検出・原因特定・ゲート適用の各ロジック）。結合（`/hawkeye-run`のサイクル全体でoverreaction候補が審理枠に正しく混ざること）。実環境確認は次回`/hawkeye-run`実行時。<br>**停止条件**: CLAUDE.mdの共通停止条件に加え、`CatalystType.EARNINGS_OVERREACTION`という名称が「決算による過剰反応」を暗示し、決算と無関係な急落（訴訟・製品リコール等）を含む本タスクの設計と食い違う場合、名称・enum設計の見直しをUser確認。 | - |
