# OpenHands Resolver プロンプト仕様

## 概要：2層アーキテクチャ

Resolver は各タスクに対して 2 種類のプロンプトを組み合わせて使用する。

```
┌──────────────────────────────────────────────────────┐
│  System Prompt (CodeActAgent)                        │
│  → 「あなたは OpenHands エージェントです...」         │
├──────────────────────────────────────────────────────┤
│  Conversation Instructions（how: どう振る舞うか）    │
│  → 「人間に質問しない」「インデントを守る」           │
│  → {{ repo_instruction }} が注入される               │
├──────────────────────────────────────────────────────┤
│  User Instruction（what: 何をするか）                │
│  → 「/workspace の Issue を修正してください」         │
│  → {{ body }} に Issue タイトル＋本文＋コメント       │
└──────────────────────────────────────────────────────┘
```

---

## 実際のプロンプトテキスト

### Issue 解決（デフォルト）

**ユーザー指示: `prompts/resolve/basic-with-tests.jinja`**

```
Please fix the following issue for the repository in /workspace.
An environment has been set up for you to start working. You may assume all necessary tools are installed.

# Problem Statement
{{ body }}
```

`{{ body }}` の展開内容:
- `issue.title + "\n\n" + issue.body`
- コメントがある場合: `+ "\n\nIssue Thread Comments:\n" + "\n---\n".join(issue.thread_comments)`

**行動指示: `prompts/resolve/basic-with-tests-conversation-instructions.jinja`**

```
IMPORTANT: You should ONLY interact with the environment provided to you AND NEVER ASK FOR HUMAN HELP.
You SHOULD INCLUDE PROPER INDENTATION in your edit commands.

Some basic information about this repository:
{{ repo_instruction }}

For all changes to actual application code (e.g. in Python or Javascript), add an appropriate
test to the testing directory to make sure that the issue has been fixed.
Run the tests, and if they pass you are done!
You do NOT need to write new tests if there are only changes to documentation or configuration files.

When you think you have fixed the issue through code changes, please call the finish action to end the interaction.
```

### テストなし版: `basic.jinja` / `basic-conversation-instructions.jinja`

上記と同じ構造だが、「テストを書いて実行する」の指示が省かれたシンプル版。

### PR レビュー対応: `basic-followup.jinja`

```
Please fix the code based on the following feedback

# Review comments
{{ review_comments }}

# Review threads
{{ review_threads }}

# Review thread files
{{ files }}

# PR Thread Comments
{{ thread_context }}
```

---

## プロジェクト独自の指示を注入する方法（3種類）

| 方法 | ファイル | 効果 | 推奨度 |
|---|---|---|---|
| **常時注入 A** | `.openhands_instructions`（リポジトリルート） | `{{ repo_instruction }}` として毎回注入 | ✅ 確実 |
| **常時注入 B** | `AGENTS.md` / `.openhands/microagents/repo.md` | `<REPOSITORY_INSTRUCTIONS>` ブロックに追加 | ✅ 確実 |
| **キーワードトリガー** | `.openhands/microagents/*.md`（frontmatter に `triggers:` 設定） | 該当キーワードが出たときだけ `<EXTRA_INFO>` として注入 | 補助的 |

> `.openhands_instructions` と `AGENTS.md` の両方が存在する場合は両方注入される。

---

## プロンプト完全カスタマイズ（`--prompt-file`）

Resolver の `--prompt-file` 引数で Issue 用プロンプトを完全に差し替え可能。

```bash
python -m openhands.resolver.resolve_issue \
  --prompt-file /path/to/my-prompt.jinja \
  ...
```

**必須**: `my-prompt.jinja` と同じディレクトリに
`my-prompt-conversation-instructions.jinja` も配置すること（自動的に読み込まれる）。

`app.py` の `run_resolver()` から渡す場合:

```python
# run_resolver() の cmd リストに追加
"--prompt-file", "/path/to/my-prompt.jinja",
```

---

## 注入されるコンテキスト全体

| データ | 取得元 | プロンプト内の位置 |
|---|---|---|
| Issue タイトル・本文 | GitLab API | `{{ body }}` |
| Issue コメント一覧 | GitLab API | `{{ body }}` に追記 |
| リポジトリ指示 | `.openhands_instructions` | `{{ repo_instruction }}` |
| `AGENTS.md` 等 | リポジトリ内ファイル | `<REPOSITORY_INSTRUCTIONS>` |
| キーワードMicroagent | リポジトリ内ファイル | `<EXTRA_INFO>` |
| 実行日時 | Runtime | `<RUNTIME_INFORMATION>` |
| PR レビューコメント（PR フローのみ） | GitLab API | `{{ review_comments }}` 等 |

---

## Issue → MR を確実に出力させるための推奨指示

### 問題：なぜ MR が作られないことがあるか

1. **Issue の説明が曖昧**でエージェントが何を実装すべきか判断できない
2. **テストが失敗**してエージェントが `finish` を呼ばずにループする
3. **リポジトリ構造の説明がなく**、どのファイルを変更すべきか探索に時間がかかりタイムアウト
4. **コミット・ブランチ操作の指示がない**ため変更をステージングしない

### Issue の書き方ガイドライン

MR を確実に生成させるには、Issue に以下を含めると効果的:

```markdown
## やること
- [ ] xxx 機能を追加する

## 受け入れ条件
- ○○ができること
- △△ のテストが通ること

## 技術的な手がかり
- 変更対象ファイル: `src/foo.py`
- 参考にすべき既存実装: `src/bar.py`
```

### `AGENTS.md` に書くべき推奨指示

以下を各リポジトリの `AGENTS.md` に記載することで、MR 生成の確実性が上がる。

```markdown
# OpenHands エージェント向け指示

## 実装完了の定義
- コードの変更が完了したら必ず `git add` と `git commit` を実行すること
- コミットメッセージは日本語で「feat: ○○機能を追加」のように書くこと
- テストが存在する場合は実行し、パスを確認してから finish すること
- テストが存在しない場合はテストを書かずに finish してよい

## 技術スタック
（プロジェクトに合わせて記載）
- 言語: Python 3.12
- フレームワーク: FastAPI
- テスト: pytest

## ディレクトリ構成
（プロジェクトに合わせて記載）
- `src/`: アプリケーションコード
- `tests/`: テストコード
- `docs/`: ドキュメント

## 禁止事項
- `migrations/` を直接編集しない（alembic を使う）
- 既存のAPIレスポンス形式を変更しない
- 認証・認可ロジックを変更しない

## コーディング規約
- 型ヒントを必ず付ける
- 公開関数には docstring を書く
- 1関数は50行以内を目安にする
```

### 最大反復数（`--max-iterations`）の調整

デフォルトは少ない場合があり、複雑な Issue でタイムアウトして MR が作られないことがある。
`app.py` の `run_resolver()` に追加:

```python
"--max-iterations", "50",  # デフォルトは 30
```

### テスト失敗ループの回避

テストがないリポジトリで `basic-with-tests.jinja` を使うと、
「テストを書いて実行する」指示に縛られてループしやすい。

対策: `--prompt-file` でテストなし版に切り替える:

```python
"--prompt-file", "/app/.venv/lib/python3.13/site-packages/openhands/resolver/prompts/resolve/basic.jinja",
```

または `AGENTS.md` に明記:

```markdown
## テストについて
このリポジトリにはテストが存在しない。
テストの作成は不要。実装が完了したら即座に finish すること。
```

---

## 参考：プロンプトファイルの場所（コンテナ内）

```
/app/openhands/resolver/prompts/
├── resolve/
│   ├── basic.jinja                                  # Issue用（テストなし）
│   ├── basic-conversation-instructions.jinja
│   ├── basic-with-tests.jinja                       # Issue用（テストあり）← デフォルト
│   ├── basic-with-tests-conversation-instructions.jinja
│   ├── basic-followup.jinja                         # PR レビュー対応用
│   ├── basic-followup-conversation-instructions.jinja
│   └── pr-changes-summary.jinja
└── guess_success/
    ├── issue-success-check.jinja                    # 成功判定
    ├── pr-feedback-check.jinja
    ├── pr-review-check.jinja
    └── pr-thread-check.jinja
```
