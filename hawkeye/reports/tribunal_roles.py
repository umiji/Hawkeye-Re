"""Generate the readable version of the tribunal's judgment criteria.

`prompts.py` is the source of truth and stays that way. It is not moved into
files a reader could browse, because a prompt rule and the code enforcing it
only mean something together — JUDGE_SYSTEM's "do not BUY over an unaddressed
severe attack" is mechanically enforced by `_judge_rule_check` (invariant 3),
and splitting them invites one to be edited while the other keeps enforcing
the old thing. API mode and session mode also read the same constant, which
is the reason their results can be compared at all.

That leaves a real problem: the standards the system actually judges by are
buried in a Python module, so a decision-maker reading `strategy/` never
finds them. This module renders them into `strategy/TRIBUNAL_ROLES.ja.md` —
prompts verbatim, with a Japanese gloss of what each role is for and which
rules the code enforces mechanically. `hawkeye docs tribunal-roles --check`
(and a test) fail when the two drift.
"""
from __future__ import annotations

import re

from hawkeye.tribunal import prompts

DOC_PATH = "strategy/TRIBUNAL_ROLES.ja.md"

_RULE_LINE = re.compile(r"^(\d+)\. ", re.MULTILINE)


def judge_rule_numbers() -> list[int]:
    """The numbered decision rules in JUDGE_SYSTEM, in order.

    These are the ones that overturn a verdict, so a new one appearing
    without a gloss has to be an error rather than a silent omission.
    """
    body = prompts.JUDGE_SYSTEM.split("Pre-registered decision rules", 1)[-1]
    return [int(n) for n in _RULE_LINE.findall(body)]


# Japanese gloss per Judge rule. Keyed by rule number so a renumbering or an
# addition in the prompt surfaces as a failure instead of a mismatch nobody
# notices.
_JUDGE_RULE_JA: dict[int, str] = {
    1: "**既定はPASS(見送り)。** BUYを出すには積極的な理由が要りますが、"
       "見送りに理由は要りません。候補は明日も来るので、迷ったら見送るのが"
       "正しく、見送ったことへの減点もありません。",
    2: "**重大な反論(severity 4以上)は、1件残らず名指しで答えなければ"
       "BUYを出せません。** 反論には内容から機械的に決まるID(`attack_id`)が"
       "付いており、Judgeはそれを引用して「反証した」または「撤退条件に"
       "変換した」のどちらかを示す必要があります。1件でも引用漏れがあれば"
       "コード側が自動的にPASSへ転覆させます(不変条件3)。"
       "**言い換えても構いません** — 一致を見るのは文言ではなくIDです。",
    3: "**優位性の正体を名指しできていない(`edge_type=none_identified`)、"
       "または「なぜ今売っている側が間違っているのか」に答えられていない"
       "場合は、PASS。**",
    4: "**確信度(`conviction`)は「気合い」ではなく確率。** 後で答え合わせ"
       "され採点されます。**弁論の勝ち負けで決めることは禁止**で、"
       "反証できずに監視条件(kill criterion)へ転換した重大な指摘は、"
       "「起きる確率×起きたときの損失」の分だけ確信度を割り引く形で払います"
       "——それだけを理由に自動的にPASSにしてはいけません(2026-08-17に"
       "旧ルール3「空売り側の主張のほうが説得的ならPASS」を廃止し、"
       "このルールへ統合)。割り引いた後の数字が0.65を下回るなら"
       "BUYと言うのは矛盾なので、どちらかに寄せる必要があります。"
       "この0.65の下限だけは`_judge_rule_check`がコードで機械的に"
       "検算します(不変条件3)。",
    5: "**採算のハードル(リワード/リスク比・期待値)はJudgeの後段で"
       "リスク審査が機械的に検査します。** 数字を通すために判断を曲げては"
       "いけません — Judgeが見るのは論の強さだけです。",
}

_HEADER = """\
# 審査3役の判断基準

> ⚠️ **この文書は `hawkeye/tribunal/prompts.py` から自動生成されています。**
> 直接編集しないでください — 次の生成で上書きされます。判断基準を変えたい
> 場合は `prompts.py` を編集し、`hawkeye docs tribunal-roles --write` で
> 再生成してください。内容がずれたままだとテストが落ちます。

Hawkeyeは1銘柄を**3つの役**に順番に審査させます。3役は**同じ会話を共有せず**、
それぞれ**見てよい材料が違います**。人間のチームが「言い出しっぺの顔を立てる」
「一度買うと好きになる」といった形で崩れる部分を、**情報の遮断**という機械的な
仕組みで置き換えるためです。

| 役 | 何をする役か | 見えるもの | 見えないもの |
|---|---|---|---|
| **Bull(強気側)** | 買う理由だけを、可能な限り強く組み立てる | 銘柄資料とゲート結果 | 反論、Judgeの判断 |
| **Adversary(反論側)** | その主張を壊すことだけを行う | 銘柄資料とゲート結果、**書かれた主張** | Bullの思考過程、Judgeの判断 |
| **Judge(裁定側)** | 記録だけを読んでBUYかPASSかを決める | 資料・ゲート結果・主張・反論 | 3役の会話、価格を通すための都合 |

Adversaryが見るのは**書かれた主張だけ**で、Bullが「本当は自信がなかった」と
いった事情は一切渡りません。Judgeが見るのも**記録だけ**です。**新しい事実を
持ち込むことは禁止**されています。

なお、API方式(従量課金のキーで回す)では3役が**技術的に独立した3回の呼び出し**
になるため遮断は保証されます。セッション方式(Claude Code内で回す)では、
**各役に渡すファイルの中身**はコードが決めますが、進行役のセッション自身は
ファイルシステムに触れるため、規律で守っている部分が残ります(不変条件4)。

---

"""

_ROLE_INTROS: dict[str, str] = {
    "bull": """\
## 1. Bull(強気側)— 買う理由を組み立てる役

**この役の目的**: 「一番良く言えばどうなるか」を、事実の範囲で言い切らせること。
弱い材料しかないのに強気に見せかけると、後の答え合わせで自分の点数が下がる
設計にしてあるため、**無理な主張をするより「優位性なし」と正直に言うほうが得**
になります。

要求している中身は次の6つです。

1. **優位性(edge)の正体**: なぜ今この歪みが存在し、なぜまだ他の参加者に
   埋められていないのか
2. **反対側の当事者**: この価格で誰が売っていて、その人はなぜ間違っているのか。
   「誰も気づいていない」は流動性のある銘柄では答えになりません
3. **検証可能な予測(claims)を3〜6件**: それぞれに確率・期間・確認方法を付ける。
   「株価が上がる」ではなく**世界について**の予測であること。確率は後で採点
   されるので、コイン投げに0.9と言うほうが0.55と言うより損をします
4. **シナリオ**: 弱気/標準/強気の確率合計が1、標準ケースは保守的に
5. **撤退条件(kill criteria)**: **今の時点で**客観的に決める。価格による損切り
   水準と時間切れを最低1つずつ。壊れ方を定義できない主張は主張ではありません
6. **想定保有日数**

### システムプロンプト(原文 — この役に固有の部分。冒頭には上の「共通部分」が
そのまま連結されます)

""",
    "adversary": """\
## 2. Adversary(反論側)— 主張を壊すことだけを行う役

**この役の目的**: 人間のチームでは「同僚に対して失礼すぎてできない」水準の
批判を、機械的に必ず実行させること。

採点は**当たった致命傷で加点、雑な難癖で減点**です。浅い反論を10個並べるより
致命的なものを3個出すほうが高く評価され、**深刻度の水増しは減点**されます
(深刻度5は「これが本当ならこの取引は死ぬ」の意味)。**主張が本当に強ければ、
反論が少ないと言うほうが評価される**設計です。

攻撃は分類に沿って網羅的に行わせます(論理の矛盾 / バリュエーションが既に
織り込んでいる成長率 / 材料の持続性 / 買い手の枯渇 / 流動性 / マクロ環境 /
決算の中身の質 / 過去の平均との整合 / 時間切れ / ガバナンスとインサイダー売り)。

必須の3項目:

1. **カモ検定(sucker test)**: 「売っている側こそ情報を持っていて、買う我々が
   カモである」という側から論じ直させる
2. **確率の監査**: Bullが最も自信過剰な予測を1つ挙げ、冷徹な賭け屋なら
   いくらを付けるかを言わせる
3. **最強の空売り論**: 藁人形ではなく、本気で説得力のある空売り側の主張を書かせる

### システムプロンプト(原文 — この役に固有の部分。冒頭には上の「共通部分」が
そのまま連結されます)

""",
    "judge": """\
## 3. Judge(裁定側)— 記録だけで決める役

**この役の目的**: 判断を「その場の納得感」ではなく**記録に対する検査**にすること。
Judgeは資料・ゲート結果・主張・反論だけを読み、**新しい事実を持ち込めません**。

判断ルールは事前に登録されており、Judgeを拘束します。**このうちいくつかは
プロンプトでお願いしているだけでなく、コードが機械的に強制**します
(不変条件3「コードがプロンプトの要求を強制する」)。

""",
}

_JUDGE_OUTRO = """\
判定文(`rationale`)には、**買う最強の理由 → 生き残った最強の反対理由 →
なぜ前者が上回るか**を、この順で書かせています。

### システムプロンプト(原文 — この役に固有の部分。冒頭には上の「共通部分」が
そのまま連結されます)

"""

_SHARED_INTRO = """\
## 0. 3役に共通して渡している前提

役に関係なく、全員が同じ**基準となる確率(base rate)**を前提に置きます。
「今回は特別だ」という理由づけを封じるためです。特に効いているのは次の点です。

- 決算後のドリフト(PEAD)は実在するが**穏やか** — 数十%ではなく数%規模
- 材料型の取引の大半は4〜8週間で±15%の範囲に収まる
- **よく選んだ取引でもおよそ半分は負ける。** 勝率ではなく、
  **損小利大の非対称性と規律**が収益源
- 流動性の高い大型株で「市場が明らかな何かを見落としている」という話は
  たいてい間違い

また、**欠損値を「ゼロ」や「異常なし」と読み替えることを禁止**しています。
インサイダー動向やアナリスト評価が空欄なのは「取得できなかった」という意味で
あって「動きがなかった」ではありません(不変条件6と同じ考え方)。

さらに、**その銘柄が属する業界そのものの値動き**(`sector_context`)も
3役全員に渡しています。銘柄が決算日に+8%動いたとき、それが会社固有の材料に
よるものか、業界全体が+7%上がった流れに乗っただけかを、渡された数字だけでは
区別できなかったためです。業界の代表ETF(例: 半導体・ソフトウェアなどの
情報技術セクターなら `XLK`)を同じ期間で測り、差分(`excess_*`)まで計算済み
で渡します。業界が特定できない場合やETFの株価が取れない場合はこの項目ごと
渡されず、それは「業界並みに動いた」ではなく「未確認」を意味します。

### 共通部分(原文)

"""


class TribunalDocError(RuntimeError):
    """The prompts changed in a way the generated document cannot describe."""


def _fenced(text: str) -> str:
    return "```text\n" + text.strip() + "\n```\n"


def role_suffix(system: str) -> str:
    """The part of a role prompt that is not the shared preamble.

    Every role prompt is `_SHARED_DOCTRINE + <role text>`, so printing all
    three in full would repeat the same preamble four times in one document
    and bury the differences — which are the only thing a reader is here
    for. The shared block is shown once, up front; each role shows its own
    half. Concatenating the two reproduces the prompt exactly, and
    `test_every_prompt_is_reproduced_verbatim` checks that it still does.
    """
    if not system.startswith(prompts._SHARED_DOCTRINE):
        raise TribunalDocError(
            "a role prompt no longer starts with _SHARED_DOCTRINE — the "
            "document assumes every role shares one preamble, so this needs "
            "a look rather than a silent full-text dump")
    return system[len(prompts._SHARED_DOCTRINE):]


def _judge_rules_ja() -> str:
    numbers = judge_rule_numbers()
    missing = [n for n in numbers if n not in _JUDGE_RULE_JA]
    if missing:
        raise TribunalDocError(
            f"JUDGE_SYSTEM has decision rules with no Japanese gloss: "
            f"{missing}. Add them to _JUDGE_RULE_JA in "
            "hawkeye/reports/tribunal_roles.py — a rule that binds the Judge "
            "must not be invisible to the reader.")
    extra = [n for n in _JUDGE_RULE_JA if n not in numbers]
    if extra:
        raise TribunalDocError(
            f"_JUDGE_RULE_JA describes rules {extra} that JUDGE_SYSTEM no "
            "longer has — remove them rather than documenting a rule nobody "
            "enforces.")
    return "\n".join(f"- **判断ルール{n}**: {_JUDGE_RULE_JA[n]}" for n in numbers)


def render_tribunal_roles_ja() -> str:
    """Build the whole document. Raises when a prompt rule has no gloss."""
    parts = [
        _HEADER,
        _SHARED_INTRO,
        _fenced(prompts._SHARED_DOCTRINE),
        "\n---\n\n",
        _ROLE_INTROS["bull"],
        _fenced(role_suffix(prompts.BULL_SYSTEM)),
        "\n---\n\n",
        _ROLE_INTROS["adversary"],
        _fenced(role_suffix(prompts.ADVERSARY_SYSTEM)),
        "\n---\n\n",
        _ROLE_INTROS["judge"],
        _judge_rules_ja(),
        "\n\n",
        _JUDGE_OUTRO,
        _fenced(role_suffix(prompts.JUDGE_SYSTEM)),
    ]
    return "".join(parts)
