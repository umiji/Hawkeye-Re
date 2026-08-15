# Task List

進捗管理の唯一の正本（single source of truth）。新規タスクの受付・登録手順は
`hawkeye-task-cycle` Skill（`.claude/skills/hawkeye-task-cycle/SKILL.md`）と
`docs/task-template.md` を参照。1タスクずつ進行し、完了条件を満たすまで
次のタスクに着手しない。

## ID 採番ルール

- 形式は `T-###`（3桁ゼロ埋め、連番）。
- 一度発行した ID は再利用しない。中止・保留になったタスクも欠番のまま
  この表に残し、削除しない。

## 状態の選択肢

`未着手` / `調査中` / `実装中` / `ローカル検証済み` / `実環境検証待ち` /
`完了` / `保留` / `中止`

## タスク一覧

| ID | タスク | 状態 | 進捗 | 依存 | 完了条件 | 証拠 |
|----|--------|------|------|------|----------|------|
| T-000 | (サンプル行) タスクの記載形式の例。実タスク登録時に置き換えるか、新規行を追加する | 未着手 | 0% | - | 完了条件をここに具体的なコマンド/出力で明記する | - |
| T-001 | Judgeのルール3・5を見直し、弁論勝負ではなく期待値ベースの判断に一本化する（`docs/design/RETURN_TARGET_STRUCTURAL_GAPS.ja.md` 障害C対応）。**目的**: ルール3（Adversaryの短い主張がBullの長い主張より説得力があればPASS、という弁論の勝敗ルール）が、severity4以上の攻撃を監視条件に転換してもなお機械的にPASSへ誘導しており、実績19件全件PASSという極端な結果になっている。放置すると審理から一切BUYが出ず、年50%目標の達成が構造的に不可能。**変更範囲**: `hawkeye/tribunal/prompts.py`（JUDGE_SYSTEM: ルール3を独立ルールとして撤廃し、「未反証だが転換済みのseverity4以上の攻撃は確信度から相応の割引を行う」という指示にルール5側で統合）／`hawkeye/tribunal/pipeline.py`（`_judge_rule_check`の確信度閾値を0.55→0.65に変更。ロジック自体は変えず定数のみ）／`strategy/VERIFICATION_PROTOCOL.md`（ルール3・5の記述更新、「enforced twice」の記述精度も見直す）／`hawkeye docs tribunal-roles --write`によるドキュメント再生成。**禁止事項**: ルール1（デフォルトPASS）・ルール2（severity4以上の攻撃の未対応チェック）・ルール4（Risk Officerの経済性チェック）はスコープ外。`config.py`の他の閾値（`min_reward_risk`等、障害A関連）は触らない。 | 未着手 | 0% | - | ① `prompts.py`のJUDGE_SYSTEMからルール3の弁論勝負の文言が消え、確信度割引の指示に置き換わっている ② `_judge_rule_check`の確信度チェックが0.65を使っている ③ `hawkeye docs tribunal-roles --check`が通る ④ 既存tribunalテストスイートが全件成功する ⑤ 新設計の挙動を検証する新規テスト（severity4攻撃を転換した上で確信度0.65超→BUY、0.65以下→PASSの両ケース）が追加され成功する<br>**テスト方法**: 単体（ScriptedLLM/StaticProviderでのオフラインテスト）。実環境確認は次回`/hawkeye-run`実行時にJudgeの実際のconviction分布を観察（即時検証不可のため継続観察）。<br>**停止条件**: CLAUDE.mdの共通停止条件のみ。 | - |
| T-002 | 候補銘柄のセクターETF値動きを審理3役の判断材料に追加する（セッション方式のみ、`docs/design/RETURN_TARGET_STRUCTURAL_GAPS.ja.md` 障害F対応）。**目的**: 審理3役に業界・セクターの比較材料が一切無く、銘柄固有の反応か業界全体の流れかを区別できない。**変更範囲**: 新規セクター→ETF変換表（主要セクターのみ、GICS11セクター相当）／既存OHLCV取得（Yahoo/Finnhub）を再利用したETF値動き取得処理／`hawkeye/contracts/models.py`（`CandidateBrief`への新フィールド追加）／`hawkeye/scout/`配下のdossier組み立て箇所への配線／`hawkeye/tribunal/prompts.py`（新フィールドの説明追記）／`hawkeye/tribunal/casefile.py`（セッション方式のパッケージ出力への反映）。**禁止事項**: 今回はセッション方式のみ対応、API方式（`hawkeye/tribunal/llm.py`経由）への配線はスコープ外（`CandidateBrief`拡張自体はモデル共有により自然に波及しうる点に留意）。主要セクター以外（Finnhubの細かい業種分類）への個別対応はスコープ外。既存`CandidateBrief`の他フィールドの意味は変えない。 | 未着手 | 0% | - | ① 主要セクター（GICS11セクター相当）のETFマッピング表が実装されている ② `CandidateBrief`に新フィールドが追加され、`casefile.write_package()`のセッション方式dossier出力に反映されている ③ 新規ユニットテスト（セクター名→ETFコード変換、ETF値動き計算）が追加され全件成功する ④ 既存テストスイートが全件成功する<br>**テスト方法**: 単体（変換関数・計算関数）＋結合（`build_brief`からdossier出力までの一連の流れ）。実環境確認は次回`/hawkeye-run`実行時。<br>**停止条件**: CLAUDE.mdの共通停止条件に加え、Finnhubの`finnhubIndustry`分類が主要セクターのどれにも当てはまらない業種が多数見つかった場合、対応方針をUser確認。 | - |
