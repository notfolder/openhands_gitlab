#!/usr/bin/env bash
# =============================================================================
# OpenHands + GitLab 初期セットアップスクリプト (Mac / Linux 対応)
# =============================================================================
# 実行順序:
#   1. docker compose up -d gitlab    # GitLabだけ先に起動
#   2. ./scripts/setup.sh             # このスクリプトを実行
#   3. docker compose up -d           # 全サービスを起動
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"

# ─── カラー出力 ────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()   { error "$*"; exit 1; }

# ─── OS 判定 & Docker ホストIP 検出 ──────────────────────────────────────────
# コンテナからホスト（GitLab）に到達するためのアドレスを決定する。
#
# Mac (Docker Desktop):
#   host.docker.internal が全コンテナで自動解決される。
#
# Linux (Docker Engine):
#   host.docker.internal は compose 管理コンテナにのみ extra_hosts で注入されるが、
#   OpenHands が動的に起動する Agent Server コンテナには届かない。
#   そのため Docker bridge ゲートウェイ IP（デフォルト 172.17.0.1）を直接使う。
#   このIPはホストのポートマッピング経由で全コンテナから到達可能。
detect_docker_host() {
    local os
    os="$(uname -s)"
    if [[ "$os" == "Darwin" ]]; then
        echo "host.docker.internal"
    else
        # Linuxでは docker0 ブリッジのゲートウェイIPを取得
        local gateway
        gateway=$(docker network inspect bridge \
            --format='{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null \
            | head -n 1 || true)
        if [[ -n "$gateway" ]]; then
            echo "$gateway"
        else
            warn "Docker bridge gateway を自動検出できませんでした。172.17.0.1 を使います"
            echo "172.17.0.1"
        fi
    fi
}

# ─── .env の指定キーを更新（または追記） ────────────────────────────────────
# Mac/Linux 両対応の sed -i 互換ラッパー
update_env_key() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        # Mac は sed -i '' が必要、Linux は sed -i のみ。一時ファイルで統一。
        local tmpfile
        tmpfile="$(mktemp)"
        sed "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" > "$tmpfile"
        mv "$tmpfile" "$ENV_FILE"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
}

# ─── .env 読み込み ─────────────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    die ".env が見つかりません。先に: cp .env.example .env して値を設定してください"
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${GITLAB_ROOT_PASSWORD:?GITLAB_ROOT_PASSWORD が .env に設定されていません}"

GITLAB_HOST="localhost:8080"
GITLAB_URL="http://${GITLAB_HOST}"

# ─── GitLab 起動確認 ──────────────────────────────────────────────────────────
wait_for_gitlab() {
    info "GitLab の起動を待っています（初回は3〜5分かかります）..."
    local retries=60
    local count=0
    until curl -sf "${GITLAB_URL}/-/health" > /dev/null 2>&1; do
        count=$((count + 1))
        if [ "$count" -ge "$retries" ]; then
            die "GitLab が起動しませんでした（${retries}回リトライ）"
        fi
        echo -n "."
        sleep 10
    done
    echo ""
    info "GitLab ヘルスチェック: OK"
}

wait_for_rails() {
    info "GitLab Rails の初期化を待っています..."
    local retries=30
    local count=0
    until docker exec gitlab gitlab-rails runner "puts 'ready'" > /dev/null 2>&1; do
        count=$((count + 1))
        if [ "$count" -ge "$retries" ]; then
            die "GitLab Rails が初期化されませんでした"
        fi
        echo -n "."
        sleep 10
    done
    echo ""
    info "GitLab Rails: 準備完了"
}

# ─── Personal Access Token 作成 ───────────────────────────────────────────────
create_pat() {
    info "Personal Access Token を作成しています..."

    local token
    token=$(docker exec gitlab gitlab-rails runner "
user = User.find_by_username('root')
existing = user.personal_access_tokens.find_by(name: 'openhands-token')
existing.revoke! if existing
new_token = user.personal_access_tokens.create!(
  name: 'openhands-token',
  scopes: ['api', 'read_repository', 'write_repository'],
  expires_at: 365.days.from_now
)
puts new_token.token
" 2>/dev/null | tail -n 1)

    if [ -z "$token" ]; then
        die "Personal Access Token の作成に失敗しました"
    fi

    echo "$token"
}

# ─── テストプロジェクト作成 ───────────────────────────────────────────────────
create_test_project() {
    local token="$1"
    local project_name="${2:-openhands-test}"

    info "テストプロジェクト '${project_name}' を作成しています..."

    local response
    response=$(curl -sf \
        --request POST \
        --header "Authorization: Bearer ${token}" \
        --header "Content-Type: application/json" \
        --data "{
            \"name\": \"${project_name}\",
            \"description\": \"OpenHands 連携テスト用プロジェクト\",
            \"visibility\": \"private\",
            \"initialize_with_readme\": true,
            \"default_branch\": \"main\"
        }" \
        "${GITLAB_URL}/api/v4/projects" 2>&1 || echo "")

    local project_id
    project_id=$(echo "$response" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); print(d['id'])" 2>/dev/null || echo "")

    local project_path
    project_path=$(echo "$response" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); print(d['path_with_namespace'])" 2>/dev/null || echo "")

    if [ -z "$project_id" ]; then
        warn "テストプロジェクトの作成に失敗しました（すでに存在する場合は既存のIDを取得します）"
        local search_response
        search_response=$(curl -sf \
            --header "Authorization: Bearer ${token}" \
            "${GITLAB_URL}/api/v4/projects?search=${project_name}" 2>/dev/null || echo "[]")
        project_id=$(echo "$search_response" | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    if p.get('name') == '${project_name}':
        print(p['id']); break
" 2>/dev/null || echo "")
        project_path=$(echo "$search_response" | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    if p.get('name') == '${project_name}':
        print(p['path_with_namespace']); break
" 2>/dev/null || echo "")
    fi

    if [ -z "$project_id" ]; then
        warn "プロジェクトIDを取得できませんでした。Webhook登録をスキップします"
        return
    fi

    info "プロジェクト作成/確認完了: ${project_path} (ID: ${project_id})"

    # 'openhands' ラベルを作成
    curl -sf \
        --request POST \
        --header "Authorization: Bearer ${token}" \
        --header "Content-Type: application/json" \
        --data '{
            "name": "openhands",
            "color": "#0075ca",
            "description": "OpenHands に処理を依頼するラベル"
        }' \
        "${GITLAB_URL}/api/v4/projects/${project_id}/labels" > /dev/null 2>&1 || true

    info "ラベル 'openhands' を作成しました"

    register_webhook "$token" "$project_id"
}

# ─── Webhook 登録 ─────────────────────────────────────────────────────────────
register_webhook() {
    local token="$1"
    local project_id="$2"
    # GitLab コンテナからは Docker 内部ネットワーク経由で Webhook receiver を呼ぶ
    local webhook_url="http://openhands-webhook:5000/webhook"

    info "Webhook を登録しています: ${webhook_url}"

    # 既存の openhands webhook を削除
    local existing_hooks
    existing_hooks=$(curl -sf \
        --header "Authorization: Bearer ${token}" \
        "${GITLAB_URL}/api/v4/projects/${project_id}/hooks" 2>/dev/null || echo "[]")

    echo "$existing_hooks" | python3 -c "
import sys, json
for h in json.load(sys.stdin):
    if 'openhands-webhook' in h.get('url', ''):
        print(h['id'])
" 2>/dev/null | while read -r hook_id; do
        curl -sf \
            --request DELETE \
            --header "Authorization: Bearer ${token}" \
            "${GITLAB_URL}/api/v4/projects/${project_id}/hooks/${hook_id}" > /dev/null 2>&1 || true
        info "既存の Webhook (ID: ${hook_id}) を削除しました"
    done

    local hook_response
    hook_response=$(curl -sf \
        --request POST \
        --header "Authorization: Bearer ${token}" \
        --header "Content-Type: application/json" \
        --data "{
            \"url\": \"${webhook_url}\",
            \"token\": \"${WEBHOOK_SECRET:-}\",
            \"issues_events\": true,
            \"note_events\": true,
            \"merge_requests_events\": true,
            \"push_events\": false,
            \"enable_ssl_verification\": false
        }" \
        "${GITLAB_URL}/api/v4/projects/${project_id}/hooks" 2>&1 || echo "")

    local hook_id
    hook_id=$(echo "$hook_response" | python3 -c \
        "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null || echo "")

    if [ -n "$hook_id" ]; then
        info "Webhook 登録完了 (ID: ${hook_id})"
    else
        warn "Webhook の登録に失敗した可能性があります（全サービス起動後に再実行してください）"
    fi
}

# ─── メイン処理 ───────────────────────────────────────────────────────────────
main() {
    echo ""
    echo "=============================================="
    echo " OpenHands + GitLab セットアップ"
    echo "=============================================="
    echo ""

    # OS 判定と Docker ホスト IP の決定
    local os
    os="$(uname -s)"
    info "OS: ${os}"

    local docker_host
    docker_host=$(detect_docker_host)
    info "Docker ホストアドレス: ${docker_host}"

    # DOCKER_HOST_INTERNAL を .env に保存（docker-compose.yml / webhook が参照する）
    update_env_key "DOCKER_HOST_INTERNAL" "${docker_host}"
    info ".env に DOCKER_HOST_INTERNAL=${docker_host} を保存しました"

    wait_for_gitlab
    wait_for_rails

    local token
    token=$(create_pat)
    info "PAT 作成完了: ${token:0:10}..."

    update_env_key "GITLAB_TOKEN" "${token}"
    info ".env に GITLAB_TOKEN を保存しました"

    create_test_project "$token" "openhands-test"

    echo ""
    echo "=============================================="
    info "セットアップ完了！"
    echo ""
    echo "次のコマンドで全サービスを起動してください:"
    echo ""
    echo "  docker compose up -d"
    echo ""
    echo "アクセス先:"
    echo "  GitLab:    http://localhost:8080  (root / ${GITLAB_ROOT_PASSWORD})"
    echo "  OpenHands: http://localhost:3000"
    echo "  Webhook:   http://localhost:5000/health"
    echo ""
    echo "テスト方法:"
    echo "  1. http://localhost:8080/root/openhands-test にアクセス"
    echo "  2. Issues > New Issue で Issue を作成"
    echo "  3. ラベル 'openhands' を付けると自動的に処理が開始されます"
    echo "  4. または Issue/MR のコメントに '@openhands' を含めると起動します"
    echo "=============================================="
}

main "$@"
