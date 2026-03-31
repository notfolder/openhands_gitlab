# OpenHands + Self-hosted GitLab 連携検証環境

self-hosted の GitLab と self-hosted の OpenHands を Docker Compose で構築し、
GitLab の Issue / MR をトリガーに OpenHands Resolver が自動実装・MR 作成を行う環境です。

---

## 構成概要

```text
[ブラウザ]
    │
    ├── localhost:3000 ──► [OpenHands UI]
    └── localhost:5000 ──► [Webhook Receiver]
    （ローカルモード時のみ）
    └── localhost:8080 ──► [GitLab CE]

[GitLab（ローカル or 外部）]
    │ グループ Webhook（グループ配下の全プロジェクトに適用）
    │ Issue ラベル / コメントイベント
    ▼
[Webhook Receiver (openhands-webhook)]
    │ docker run
    ▼
[OpenHands Resolver コンテナ（動的生成）]
    │ GitLab API + git clone/push
    ▼
[Agent Server コンテナ（動的生成）] ──► コード実装 ──► Draft MR 作成
```

### サービス一覧

| サービス | イメージ | ポート | 起動条件 | 役割 |
| --- | --- | --- | --- | --- |
| gitlab | `gitlab/gitlab-ce:latest` | 8080 / 2222 | `--profile local` 指定時のみ | ソースコード管理・Issue管理 |
| openhands | `docker.openhands.dev/openhands/openhands:1.5` | 3000 | 常時 | OpenHands Web UI |
| openhands-webhook | `./webhook`（独自ビルド） | 5000 | 常時 | GitLab Webhook 受信 → Resolver 起動 |
| openhands-resolver-image | `./resolver`（独自ビルド） | - | ビルド専用（`--profile build-only`） | Resolver 用イメージのビルド定義（実コンテナは docker run で動的生成） |

---

## 動作フロー

### Issue ラベルトリガー

```text
1. Issue に "openhands" ラベルを付与（グループレベルの共通ラベル）
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

### グループ横断対応

- グループレベルのラベル `openhands` が全プロジェクトで共通利用できる（GitLab CE でも動作）
- Webhook はプロジェクト単位で登録（GitLab CE ではグループ Webhook が使えないため）
- 新規プロジェクト追加時は `./scripts/setup.sh --add-project` で Webhook を登録

---

## 起動手順

### 前提条件

- Docker Desktop (Mac) または Docker Engine + Docker Compose v2 (Linux)
- Python 3.x（`setup.sh` が `python3` を使用）

---

### Step 0: GitLab を用意する

#### ローカル GitLab を使う場合（オプション）

ローカルで GitLab を起動する場合は以下を実行します。外部の GitLab を使う場合はこの手順をスキップしてください。

```bash
cp .env.example .env
```

`.env` を編集して GitLab の初期パスワードを設定：

```bash
GITLAB_ROOT_PASSWORD=Password1234!  # 8文字以上、英数字+記号
```

GitLab を起動：

```bash
docker compose --profile local up -d gitlab
```

> 初回起動は 3〜5 分かかります。起動完了まで `docker logs -f gitlab` で確認してください。

---

### Step 1: openhands ユーザーを作成し PAT を発行する

GitLab（ローカルは `http://localhost:8080`）にアクセスし、`openhands` 専用ユーザーを作成します。

#### ユーザー作成

- **GitLab が自己登録を許可している場合**: トップページから `openhands` ユーザーとして登録
- **管理者権限がある場合**: Admin Area → Users → New User で作成
- **外部 GitLab の場合**: 管理者に `openhands` ユーザーの作成を依頼、または自己登録

#### PAT（Personal Access Token）を発行

`openhands` ユーザーでログインし：

```text
User Settings → Access Tokens → Add new token
名前: openhands-token（任意）
スコープ: api, read_repository, write_repository
```

発行されたトークンを控えておきます。

---

### Step 2: 環境変数を設定する

```bash
cp .env.example .env
```

`.env` を編集：

```bash
# GitLab の URL（ローカルの場合は localhost:8080）
GITLAB_EXTERNAL_URL=http://localhost:8080   # または外部GitLabのURL

# Step 1 で発行した PAT
GITLAB_TOKEN=glpat-xxxxxxxxxxxx

# LLM 設定（必須）
LLM_API_KEY=sk-...
LLM_MODEL=openai/gpt-4.1

# GitLab から Webhook Receiver に到達できる URL
# ローカル GitLab（同一 Docker ネットワーク）の場合は未設定でOK
# 外部 GitLab の場合は明示的に設定:
#   WEBHOOK_URL=http://<このホストのIP>:5000/webhook
WEBHOOK_URL=

# Webhook 認証トークン（推奨）
WEBHOOK_SECRET=random-string
```

**LiteLLM proxy を使う場合**（OpenAI 互換 API）：

```bash
LLM_API_KEY=<LiteLLM の API キー>
LLM_MODEL=openai/gpt-4.1
LLM_BASE_URL=http://<litellm-host>:4000
```

---

### Step 3: セットアップを実行する

```bash
./scripts/setup.sh
```

実行されること：

- GitLab への接続確認（`GITLAB_TOKEN` の検証）
- OS 判定 → `DOCKER_HOST_INTERNAL` を `.env` に自動書き込み
- グループ `openhands` を作成（`openhands` ユーザーが Owner として作成）
- グループレベルラベル `openhands` を作成（全プロジェクトで共通利用可能）
- テストプロジェクト `openhands/openhands-test` を作成
- テストプロジェクトにプロジェクト Webhook を登録
- `GITLAB_BASE_URL` / `GIT_BASE_DOMAIN` を `.env` に自動書き込み

> **can_create_group が無効の場合**: GitLab 管理者にグループ作成と `openhands` ユーザーへの Owner 権限付与を依頼してください。その後 `GITLAB_GROUP` を既存グループのパスに設定して再実行します。

---

### Step 4: OpenHands と Webhook を起動する

**ローカル GitLab の場合:**

```bash
docker compose --profile local up -d
```

**外部 GitLab の場合:**

```bash
docker compose up -d
```

---

### Step 5: 動作確認

```bash
# Webhook の状態確認
curl http://localhost:5000/health

# ブラウザで確認（Mac）
open http://localhost:8080   # GitLab（ローカルモード時のみ）
open http://localhost:3000   # OpenHands UI
```

---

## テスト方法

### Issue でテスト

1. `{GitLab URL}/openhands/openhands-test` を開く
2. **Issues > New issue** で Issue を作成
3. ラベル `openhands` を付けて保存
4. しばらく待つと…
   - Issue にコメントが投稿される
   - `openhands-fix-issue-{N}` ブランチが作成される
   - Draft MR が作成される

### コメントでテスト

Issue または MR のコメント欄に：

```text
@openhands ログイン機能を実装してください
```

と書いて送信すると Resolver が起動します。

### 新規プロジェクトへの Webhook 追加

新規プロジェクトを作成したら以下を実行して Webhook を登録します。

```bash
./scripts/setup.sh --add-project <namespace/project>

# 例: openhands グループの my-repo プロジェクトに追加
./scripts/setup.sh --add-project openhands/my-repo

# 例: 別グループのプロジェクトに追加（openhands ユーザーが Maintainer 以上であること）
./scripts/setup.sh --add-project other-team/their-repo
```

実行されること：

- プロジェクトの存在確認
- グループレベルラベル `openhands` を追加（グループ配下の場合、既存はスキップ）
- プロジェクト Webhook を登録（既存の openhands Webhook は置き換え）

登録後の確認：

```text
http://<GitLab URL>/<namespace>/<project>/-/hooks
```

> **権限について**: `openhands` ユーザーがプロジェクトの **Maintainer 以上**の権限を持っている必要があります。グループ Owner が作成したプロジェクトには自動的に権限が付与されます。

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

`.openhands/microagents/repo.md` でも同様に常時ロードされます（旧仕様・現在も動作）。

---

## Webhook 仕様

### エンドポイント

`POST http://<webhook-host>:5000/webhook`

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
# 正常時: {"status": "ok", "trigger_label": "openhands", "trigger_mention": "@openhands", "llm_model": "..."}
# 設定不足: {"status": "warning", "missing_env": ["GITLAB_TOKEN"]}
```

### Resolver イメージ

`openhands:1.5` は `git` を含まないため、[resolver/Dockerfile](resolver/Dockerfile) で `git` を追加したカスタムイメージ `openhands-resolver:local` をビルドして使用しています。

```bash
# 初回ビルド（setup.sh 実行前に一度だけ実行）
docker build -t openhands-resolver:local ./resolver/

# ベースイメージを更新したいとき（例: openhands:1.5 → 1.x に上げた後）
# resolver/Dockerfile の FROM 行を変更してから再ビルド
docker build --no-cache -t openhands-resolver:local ./resolver/
```

### Resolver の起動パラメータ

Webhook Receiver は以下の環境変数を付けて OpenHands Resolver コンテナを `docker run` する。

| 環境変数 | 値 | 説明 |
| --- | --- | --- |
| `GITLAB_TOKEN` | OpenHands ユーザーの PAT | GitLab API 認証 |
| `GITLAB_BASE_URL` | GitLab の API エンドポイント URL | ローカル/外部で自動切替 |
| `GIT_BASE_DOMAIN` | GitLab のホスト:ポート | git clone/push のドメイン |
| `LLM_API_KEY` | API キー | LLM 認証 |
| `LLM_MODEL` | `openai/gpt-4.1` など | 使用モデル |
| `LLM_BASE_URL` | LiteLLM proxy URL など | 空なら OpenAI 直接接続 |
| `WORKSPACE_BASE` | `/tmp/openhands-resolver-workspace/{uuid}` | 作業ディレクトリ（ホストパス） |

---

## 環境変数一覧

### GitLab 接続

| 変数 | デフォルト | 説明 |
| --- | --- | --- |
| `GITLAB_EXTERNAL_URL` | - | GitLab の URL（ローカル・外部ともに設定必須） |
| `GITLAB_TOKEN` | - | `openhands` ユーザーの PAT（必須） |
| `WEBHOOK_URL` | `http://openhands-webhook:5000/webhook` | GitLab から Webhook Receiver への URL |

### LLM

| 変数 | デフォルト | 説明 |
| --- | --- | --- |
| `LLM_API_KEY` | - | API キー（必須） |
| `LLM_MODEL` | `openai/gpt-4.1` | 使用モデル |
| `LLM_BASE_URL` | 空 | LiteLLM proxy 等の URL。空なら OpenAI 直接接続 |

### GitLab 共通

| 変数 | デフォルト | 説明 |
| --- | --- | --- |
| `GITLAB_ROOT_PASSWORD` | - | ローカルモード時の root パスワード |
| `GITLAB_GROUP` | `openhands` | グループ Webhook・ラベルを設定するグループ名 |
| `OPENHANDS_USER` | `openhands` | MR 作成者として表示される専用ユーザー名 |
| `OPENHANDS_USER_EMAIL` | `openhands@localhost.local` | 専用ユーザーのメールアドレス |

### ポート（ローカルモード）

| 変数 | デフォルト | 対象 |
| --- | --- | --- |
| `GITLAB_HTTP_PORT` | `8080` | GitLab Web UI |
| `GITLAB_SSH_PORT` | `2222` | GitLab SSH |
| `OPENHANDS_PORT` | `3000` | OpenHands UI |
| `WEBHOOK_PORT` | `5000` | Webhook Receiver |

### トリガー

| 変数 | デフォルト | 説明 |
| --- | --- | --- |
| `TRIGGER_LABEL` | `openhands` | このラベルが Issue/MR に付くとトリガー |
| `TRIGGER_MENTION` | `@openhands` | コメントにこの文字列が含まれるとトリガー |
| `WEBHOOK_SECRET` | - | Webhook 認証トークン（推奨） |

---

## Mac / Linux の違い

| 項目 | Mac (Docker Desktop) | Linux (Docker Engine) |
| --- | --- | --- |
| コンテナ→ホスト到達 | `host.docker.internal`（自動解決） | Docker bridge gateway IP（例: `172.17.0.1`） |
| `DOCKER_HOST_INTERNAL` | `host.docker.internal` | 検出した gateway IP |
| 設定方法 | `setup.sh` が自動検出 | `setup.sh` が自動検出 |

---

## ログ確認

```bash
# Webhook Receiver のログ（トリガー・Resolver 起動状況）
docker logs -f openhands-webhook

# OpenHands UI のログ
docker logs -f openhands

# GitLab のログ（ローカルモード時のみ）
docker logs -f gitlab
```

---

## 停止・リセット

```bash
# 停止（データは保持）
docker compose down                        # 外部 GitLab モード
docker compose --profile local down        # ローカル GitLab モード

# 完全リセット（GitLab データも削除、ローカルモードのみ）
docker compose --profile local down -v
rm -rf /tmp/openhands-resolver-workspace

# Resolver イメージの再ビルド（resolver/Dockerfile を変更したとき）
docker build -t openhands-resolver:local ./resolver/
```

---

## 既知の制限事項

| 制限 | 内容 |
| --- | --- |
| GitLab UI モードのバグ | OpenHands UI から self-hosted GitLab に接続すると 401 エラーが発生する（Issue #8878）。Resolver/CI モードは正常動作 |
| GitLab CI トリガーテンプレートなし | GitHub Actions 相当の公式 `.gitlab-ci.yml` テンプレートは未提供（本環境は Webhook で代替） |
| MR コメントトリガーの既知バグ | MR コメントから起動した場合、デフォルトブランチをチェックアウトしてしまうケースがある（Issue #9678） |
| V0 API 廃止 | `/api/conversations` は 2026年4月1日廃止。現行は `/api/v1/app-conversations` を使用 |
