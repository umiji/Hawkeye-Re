# 環境・ツールの落とし穴

**読む契機: コマンドやツールで詰まったとき。** 症状 → 原因 → 回避策の順。

| 症状 | 原因 | 回避策 |
|---|---|---|
| sec.gov へのアクセスが**403**で全滅する | WebFetchのUser-Agentが拒否される。SECは連絡先入りのUser-Agentを要求する | 連絡先入りUser-Agentを付けた `curl`、または `hawkeye/marketdata/edgar.py` の経路を使う(2026-08-02(d)、サブエージェントが実測) |
| `git commit -m @'…'@` がpathspecエラーで壊れる | PowerShellのヒアドキュメントは本文に `"` が入ると解釈が崩れる | メッセージをファイルに書いて `git commit -F <ファイル>` |
| `python -c "…"` が "ScriptBlock should only be specified…" で落ちる | PowerShellが引用符を先に解釈する | 一時ファイル(スクラッチパッド)に `.py` を書いて実行する |
| 日本語ファイルの検索結果が化け、行番号もずれる | コンソールの文字コードと、LFのみの改行 | `Select-String` ではなく `Grep` ツールを使う |
| `hawkeye` の全コマンドが「unable to open database file」で起動即死 | `var/` を誰も作っていなかった(SQLiteはファイルは作るがフォルダは作らない)。リポジトリに `var/` が存在したため長く露見しなかった | 2026-08-02(d)に修正済み(`hawkeye/paths.py` が親ディレクトリを作る) |
| CLIの出力が `UnicodeEncodeError` で落ちる(Windows) | 標準出力が既定でシステムのコードページ(cp932) | `main()` 冒頭で標準出力/エラーをUTF-8に再設定済み(2026-07-29) |

## テストの実行

```
.venv\Scripts\python.exe -m pytest tests/ -q --ignore=tests/test_llm_auth.py
```

`.venv/bin` ではなく `.venv\Scripts`(Windows)。`test_llm_auth.py` は存在しない関数を
importする既知の収集エラーがあり、対象外にしています。テストは完全オフラインです。

`FINNHUB_API_KEY` は `.env.local` にあり、シェルの環境変数ではありません。単発
スクリプトは `from hawkeye.envfile import load_local_env; load_local_env()` を呼ぶこと。
