# OpenHands + Self-Hosted GitLab 連携調査メモ

## 構成概要

- OpenHands: v1.5.0（2026年3月11日リリース）
- GitLab: Self-hosted（バージョン指定なし）
- LLM: OpenAI API
- 連携方式: Resolver/CIパイプラインモード
- 用途: ローカル検証

---

## OpenHands のアーキテクチャ

OpenHands は Docker socket をマウントし、エージェントタスクごとにサンドボックスコンテナ（Agent Server）を動的にスポーンする構成。

```
[ユーザーブラウザ]
     ↓ port 3000
[OpenHands App コンテナ]
     ↓ /var/run/docker.sock
[Agent Server コンテナ（タスクごとに動的生成）]
     ↓
[Workspace / Git リポジトリ]
```

### 必須要件
- Docker socket マウント: `/var/run/docker.sock:/var/run/docker.sock`
- `extra_hosts: host.docker.internal:host-gateway`（ホストサービスへのアクセス用）
- 永続化ディレクトリ: `~/.openhands:/.openhands`（設定・会話履歴）
- ワークスペース: `/opt/workspace_base`（作業ディレクトリ）

---

## Docker イメージ

| 用途 | イメージ |
|---|---|
| メインアプリ | `docker.openhands.dev/openhands/openhands:1.5` |
| Agent Server | `ghcr.io/openhands/agent-server:1.12.0-python` |

---

## 主要環境変数

### コア設定

| 変数名 | 説明 | 例 |
|---|---|---|
| `LLM_MODEL` | LLMモデル識別子 | `openai/gpt-4o` |
| `LLM_API_KEY` | LLM APIキー | `sk-...` |
| `LLM_BASE_URL` | カスタムAPIエンドポイント | （任意） |
| `RUNTIME` | サンドボックス方式 | `docker`（デフォルト） |
| `WORKSPACE_MOUNT_PATH` | コンテナ内ワークスペースパス | `/opt/workspace_base` |
| `AGENT_SERVER_IMAGE_REPOSITORY` | Agent Serverレジストリ | `ghcr.io/openhands/agent-server` |
| `AGENT_SERVER_IMAGE_TAG` | Agent Serverタグ | `1.12.0-python` |

### GitLab連携（Resolver/CIモード）

| 変数名 | 説明 |
|---|---|
| `GITLAB_TOKEN` | GitLab Personal Access Token |
| `GIT_BASE_DOMAIN` | Self-hosted GitLabのドメイン（例: `gitlab.example.com`） |
| `GITLAB_BASE_URL` | Self-hosted GitLabのURL（`GIT_BASE_DOMAIN`と同等） |

---

## Self-hosted GitLab 連携の現状

### UIインタラクティブモード（非推奨）
- **既知のバグあり**（Issue #8878）: `host` フィールドが `GitLabService` に渡されず 401 エラー
- 現時点では実用的でない

### Resolver/CIパイプラインモード（推奨）
- `GIT_BASE_DOMAIN` + `GITLAB_TOKEN` で動作確認済み
- GitLabのIssueやMRをトリガーに自動処理が可能
- **注意**: GitHub Actions相当の `.gitlab-ci.yml` イベントドリブンテンプレートは公式未提供（Issue #8603がclosed/not planned）
- Webhookを自分で実装するか、手動実行が必要

---

## Issue トリガー時のワークフロー

### トリガー方法

| 方式 | GitHub（参考） | GitLab相当 |
|---|---|---|
| ラベル付与 | `fix-me` ラベル | カスタム実装必要 |
| コメントメンション | `@openhands-agent` | カスタム実装必要 |

### 処理フロー（Issue トリガー時）

1. ラベル付与またはコメントによりトリガー発火
2. OpenHands がIssueの内容を読み込み
3. 作業開始コメントをIssueに投稿（ActionのURLリンク付き）
4. `openhands-fix-issue-{番号}` という名前で新規ブランチを作成
5. エージェントが作業・コミット
6. **成功時**: Draft MR/PRを作成して元Issueにリンクコメント投稿
7. **失敗時**: ブランチのみPushし、失敗内容をIssueにコメント
8. `fix-me` ラベルをIssueから自動削除

### ラベルトリガーとコメントトリガーの違い
- **ラベル**: Issue全体のコメントスレッドを読み込む
- **コメント**: メンションしたコメントとIssue説明のみ読み込む

---

## MR コメント時のワークフロー

### 既存MRへのコメント（`@openhands-agent` メンション）

- 新しいブランチを作らず、**既存MRのブランチに直接コミット・プッシュ**
- `issue_type=pr` として検出され、`issue.head_branch` のブランチを対象にする

### サブタスク・コミット粒度

- 1回のトリガーで1つのエージェントセッションが起動
- **タスクを細分化した複数コミットは可能**（ツール呼び出し＝ファイル書き込みごとにコミット）
- ただし、1トリガー＝1セッション。複数ブランチや並列MRへの分割は自動では行われない
- 並列サブタスク処理は Sub-Agent Delegation SDK の機能（Resolverとは別）

### 既知のバグ（Issue #9678）
- PRコメントからトリガーした場合、デフォルトブランチのコードをチェックアウトし、PRブランチのコードを使わないケースがある
- 変更が重複して適用されたり `git diff` がエラーになる問題

---

## プロジェクト独自プロンプトの注入

### 推奨方法（現行 V1）

**`AGENTS.md`（リポジトリルート）**
- 最も推奨される方法
- エージェントセッション開始時に自動ロード
- 記述内容: アーキテクチャ説明・コーディング規約・テスト手順・禁止事項など

```markdown
# AGENTS.md

## プロジェクト概要
...

## コーディング規約
...

## テスト方法
...
```

**`.agents/skills/*.md`**（高度な使い方）
- キーワードトリガーで動的ロード可能
- YAML フロントマターで設定

```yaml
---
name: code-review
description: Pythonコードレビュー時に使用
triggers:
- code review
- レビュー
---
（スキル内容）
```

### レガシー方法（V0・現在も動作）

**`.openhands/microagents/repo.md`**
- フロントマター不要
- 常時ロード（`trigger_type: always`）
- Resolverの公式READMEでも案内されている方法

```
.openhands/microagents/
├── repo.md          # 常時ロード
├── git.md           # キーワードトリガー
└── testing.md       # キーワードトリガー
```

### スクリプトフック

| ファイル | タイミング | 用途 |
|---|---|---|
| `.openhands/setup.sh` | セッション開始時 | 依存インストール・環境変数設定 |
| `.openhands/pre-commit.sh` | コミット前 | Lint・テスト・品質チェック |

### ロード優先順位

1. `.agents/skills/`（現行推奨）
2. `.openhands/skills/`（非推奨エイリアス）
3. `.openhands/microagents/`（非推奨・動作は継続）

---

## API（V1 / 現行）

| Method | Endpoint | 用途 |
|---|---|---|
| `POST` | `/api/v1/app-conversations` | 会話/タスク作成 |
| `GET` | `/api/v1/app-conversations?ids=ID` | 実行ステータス取得 |
| `POST` | `/send-message` | エージェントへメッセージ送信 |
| `GET` | `/api/settings` | 設定取得 |
| `POST` | `/api/settings` | 設定更新 |
| `GET` | `/api/options/models` | 利用可能モデル一覧 |

Swagger UI: `http://localhost:3000/docs`

> **注意**: V0 API (`/api/conversations`) は **2026年4月1日廃止予定**

### 認証
- Self-hosted: `X-Session-API-Key` ヘッダー

---

## docker-compose.yml 作成時のポイント

1. **Docker socket マウント必須**
2. GitLabとOpenHandsを同一compose内に配置する場合はDockerネットワーク名で内部通信
3. Resolver/CIモードでは `GIT_BASE_DOMAIN` にGitLabコンテナのサービス名またはIPを指定
4. GitLab起動完了を待ってからOpenHandsを起動する `depends_on` と `healthcheck` の設定推奨
5. ワークスペースはNamed volumeかbind mountで永続化

---

## バージョン履歴（直近）

| バージョン | リリース日 | 主な変更 |
|---|---|---|
| 1.5.0 | 2026-03-11 | Gitリポジトリの付け替え、タスク一覧タブ、スラッシュコマンドメニュー、Bitbucket Datacenterサポート |
| 1.4.0 | 2026-02-17 | MiniMax-M2.5モデル対応 |
| 1.3.0 | 2026-02-02 | CORS対応、ホストネットワーキングモード |
| 1.2.0 | 2026-01-15 | 会話準備状況インジケーター、会話エクスポート |
| 1.1.0 | 2025-12-30 | OAuth 2.0 Device Flow（CLI）、Forgejo連携 |
| 1.0.0 | 2025-12-16 | メジャーリリース、Azure DevOpsサポート |
