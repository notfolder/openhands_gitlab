# OpenHands + Self-hosted GitLab 連携検証環境

self-hosted の GitLab と self-hosted の OpenHands を Docker Compose で構築し、
GitLab の Issue / MR をトリガーに OpenHands Resolver が自動実装・MR 作成を行う環境です。

---

## 構成概要

```text
[ブラウザ]
    │
    ├── localhost:8080 ──► [GitLab CE]
    ├── localhost:3000 ──► [OpenHands UI]
    └── localhost:5000 ──► [Webhook Receiver]

[GitLab]
    │ Issue ラベル / コメントイベント
    ▼
[Webhook Receiver (openhands-webhook)]
    │ docker run
    ▼
[OpenHands Resolver コンテナ (動的生成)]
    │ GitLab API + git clone/push
    ▼
[Agent Server コンテナ (動的生成)] ──► コード実装 ──► Draft MR 作成
```

### サービス一覧

| サービス | イメージ | ポート | 役割 |
| --- | --- | --- | --- |
| gitlab | `gitlab/gitlab-ce:latest` | 8080 (HTTP), 2222 (SSH) | ソースコード管理・Issue管理 |
| openhands | `docker.openhands.dev/openhands/openhands:1.5` | 3000 | OpenHands Web UI |
| openhands-webhook | `./webhook` (独自ビルド) | 5000 | GitLab Webhook 受信 → Resolver 起動 |

---

## 動作フロー

### Issue ラベルトリガー

```text
1. Issue に "openhands" ラベルを付与
2. GitLab → Webhook Receiver (POST /webhook)
3. Webhook Receiver が OpenHands Resolver コンテナを起動
4. Resolver が Issue 内容を読み、ブランチ openhands-fix-issue-{N} を作成
5. Agent がコードを実装・コミット
6. 成功 → Draft MR 作成 + Issue にコメント
   失敗 → ブランチのみ push + Issue にエラーコメント
```

### コメントトリガー

```text
Issue / MR のコメントに "@openhands" を含める
  → Issue コメント: 新規ブランチ作成 → 実装 → Draft MR
  → MR コメント:   既存 MR ブランチに直接コミット・push
```

---

## Webhook 仕様

### エンドポイント

`POST http://localhost:5000/webhook`

### 受信イベント

| GitLab イベント | X-Gitlab-Event ヘッダー | トリガー条件 |
| --- | --- | --- |
| Issue 作成・更新 | `Issue Hook` | `openhands` ラベルが付いている |
| Issue コメント | `Note Hook` | コメント本文に `@openhands` を含む |
| MR コメント | `Note Hook` | コメント本文に `@openhands` を含む |
| MR 作成・更新 | `Merge Request Hook` | `openhands` ラベルが付いている |

### 認証

`X-Gitlab-Token` ヘッダーに `.env` の `WEBHOOK_SECRET` と同じ値を設定。
空文字の場合は認証をスキップ（ローカル検証用）。

### ヘルスチェック

```bash
curl http://localhost:5000/health
# 正常時: {"status": "ok", "trigger_label": "openhands", "trigger_mention": "@openhands"}
# 設定不足: {"status": "warning", "missing_env": ["GITLAB_TOKEN"]}
```

### Resolver の起動パラメータ

Webhook Receiver は以下の環境変数を付けて OpenHands Resolver コンテナを `docker run` する。

| 環境変数 | 値 | 説明 |
| --- | --- | --- |
| `GITLAB_TOKEN` | PAT | GitLab API 認証 |
| `GITLAB_BASE_URL` | `http://{DOCKER_HOST_INTERNAL}:8080` | GitLab API エンドポイント |
| `GIT_BASE_DOMAIN` | `{DOCKER_HOST_INTERNAL}:8080` | git clone/push のドメイン |
| `LLM_API_KEY` | OpenAI API Key | LLM 認証 |
| `LLM_MODEL` | `openai/gpt-4o` | 使用モデル |
| `WORKSPACE_BASE` | `/tmp/openhands-resolver-workspace/{uuid}` | 作業ディレクトリ（ホストパス） |

---

## Mac / Linux の違い

| 項目 | Mac (Docker Desktop) | Linux (Docker Engine) |
| --- | --- | --- |
| コンテナ→ホスト到達 | `host.docker.internal` (自動解決) | Docker bridge gateway IP (例: `172.17.0.1`) |
| `DOCKER_HOST_INTERNAL` | `host.docker.internal` | 検出した gateway IP |
| Agent Server コンテナ | `host.docker.internal` が自動解決される | gateway IP を直接使用するため追加設定不要 |
| `extra_hosts` 設定 | 冗長だが無害 | compose 管理コンテナに `host.docker.internal` を注入 |

`setup.sh` が OS を自動判定して `DOCKER_HOST_INTERNAL` を `.env` に書き込みます。

---

## 起動手順

### 前提条件

- Docker Desktop (Mac) または Docker Engine + Docker Compose v2 (Linux)
- Python 3.x（`setup.sh` が `python3` を使用）

### Step 1: 環境変数を設定

```bash
cp .env.example .env
```

`.env` を編集して以下を設定：

```bash
OPENAI_API_KEY=sk-...        # 必須
GITLAB_ROOT_PASSWORD=...     # 8文字以上、英数字+記号
WEBHOOK_SECRET=...           # 任意の文字列（推奨）
```

### Step 2: GitLab を起動

```bash
docker compose up -d gitlab
```

> 初回起動は3〜5分かかります。

### Step 3: 初期セットアップ実行

```bash
./scripts/setup.sh
```

実行されること：

- OS 判定 → `DOCKER_HOST_INTERNAL` を `.env` に自動書き込み
- GitLab の起動・Rails 初期化を待機
- root ユーザーの Personal Access Token を作成 → `.env` の `GITLAB_TOKEN` に自動書き込み
- テストプロジェクト `root/openhands-test` を作成
- `openhands` ラベルを作成
- Webhook を GitLab プロジェクトに登録

### Step 4: 全サービスを起動

```bash
docker compose up -d
```

### Step 5: 動作確認

```bash
# Webhook サービスの疎通確認
curl http://localhost:5000/health

# GitLab にアクセス
open http://localhost:8080        # Mac
xdg-open http://localhost:8080    # Linux

# OpenHands UI にアクセス
open http://localhost:3000
```

---

## テスト方法

### Issue でテスト

1. `http://localhost:8080/root/openhands-test` を開く
2. **Issues > New issue** で Issue を作成
3. `openhands` ラベルを付けて保存
4. しばらく待つと…
   - Issue にコメント「作業を開始しました」が投稿される
   - `openhands-fix-issue-{N}` ブランチが作成される
   - Draft MR が作成される

### コメントでテスト

Issue または MR のコメント欄に:

```text
@openhands ログイン機能を実装してください
```

と書いて送信すると Resolver が起動します。

---

## プロジェクト独自プロンプトの注入

リポジトリのルートに `AGENTS.md` を置くと、OpenHands がセッション開始時に自動で読み込みます。

```markdown
# AGENTS.md

## 技術スタック
- バックエンド: FastAPI + SQLAlchemy 2
- フロントエンド: Vue 3 + Vuetify 3

## コーディング規約
- 型ヒントを必ず付ける
- テストは pytest で書く

## コミット粒度
- 各 API エンドポイント実装完了でコミット
- 各画面実装完了でコミット
```

---

## ログ確認

```bash
# Webhook Receiver のログ（トリガー・Resolver起動状況）
docker logs -f openhands-webhook

# GitLab のログ
docker logs -f gitlab

# OpenHands UI のログ
docker logs -f openhands
```

---

## 停止・リセット

```bash
# 停止（データは保持）
docker compose down

# 完全リセット（GitLab データも削除）
docker compose down -v
rm -rf /tmp/openhands-resolver-workspace
```

---

## 既知の制限事項

| 制限 | 内容 |
| --- | --- |
| GitLab UI モードのバグ | OpenHands UI から self-hosted GitLab に接続すると 401 エラーが発生する（Issue #8878）。Resolver/CI モードは正常動作 |
| GitLab CI トリガーテンプレートなし | GitHub Actions 相当の公式 `.gitlab-ci.yml` テンプレートは未提供（本環境は Webhook で代替） |
| MR コメントトリガーの既知バグ | MR コメントから起動した場合、デフォルトブランチをチェックアウトしてしまうケースがある（Issue #9678） |
| V0 API 廃止 | `/api/conversations` は 2026年4月1日廃止。現行は `/api/v1/app-conversations` を使用 |
